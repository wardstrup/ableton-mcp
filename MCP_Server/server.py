# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Optional, Union

from .telemetry import record_startup
from .telemetry_decorator import telemetry_tool, rich_telemetry_tool
from .dataset import dataset_enabled, get_recorder
from .dataset.trajectory_decorator import trajectory_tool

ABLETON_HOST = os.environ.get("ABLETON_HOST", "localhost")
ABLETON_PORT = int(os.environ.get("ABLETON_PORT", "9877"))

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AbletonMCPServer")

@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None

    # One socket, several threads: tool calls run on the MCP worker while the
    # passive poller drains events on its own thread. Serialise so their
    # sendall/recv pairs cannot interleave.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton at {self.host}:{self.port}: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        with self._lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception as e:
                    logger.error(f"Error disconnecting from Ableton: {str(e)}")
                finally:
                    self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        sock.settimeout(15.0)  # Increased timeout for operations that might take longer
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response.

        Serialised on ``self._lock``: the request and its response are one
        indivisible exchange on a shared socket. Holding the lock across the
        whole round-trip (including reconnect) is what keeps a concurrent
        caller from reading someone else's reply.
        """
        with self._lock:
            return self._send_command_locked(command_type, params)

    def _send_command_locked(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")

        command = {
            "type": command_type,
            "params": params or {}
        }
        
        # Check if this is a state-modifying command
        is_modifying_command = command_type in [
            "create_midi_track", "create_audio_track", "set_track_name",
            "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
            "delete_clip",
            "clear_notes_from_clip",
            "set_tempo", "fire_clip", "stop_clip", "set_device_parameter",
            "start_playback", "stop_playback", "load_instrument_or_effect",
            # Arrangement view commands
            "switch_to_arrangement_view", "set_current_song_time",
            "duplicate_session_clip_to_arrangement",
            "create_locator",
            # Recording / mixer commands. These return as soon as Live accepts
            # the call; they do not wait for a take to finish.
            "set_track_arm", "set_track_mute", "set_track_solo",
            "set_track_volume", "set_track_panning", "set_track_send",
            "set_track_monitoring", "set_track_input_routing",
            "set_arrangement_record", "set_session_record",
            "set_arrangement_overdub", "record_empty_slot", "stop_recording",
            "prepare_track", "set_clip_region",
            "quick_record", "quick_arrangement_record",
            "quick_arrangement_stop", "set_mixer_batch",
            "record_loop", "stop_record_loop", "stop_take",
            "set_metronome", "set_count_in", "create_scene",
            "keep_take", "list_takes",
            "show_view", "arm_tracks", "take", "again", "scrap", "cancel",
            "play_transport", "pause_transport", "nudge_tempo", "mark_playhead",
            "label_take_lane", "delete_take_lane",
            "stop_performance", "keep_last",
        ]

        # Commands whose work on Live's main thread can take noticeably longer
        # than the default modifying-command budget (e.g. importing/decoding a
        # large audio file). Give them a wider socket timeout so we don't time
        # out before the Remote Script's own queue does.
        long_running_commands = {"create_audio_clip": 65.0}
        
        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set timeout based on command type
            if command_type in long_running_commands:
                timeout = long_running_commands[command_type]
            else:
                timeout = 15.0 if is_modifying_command else 10.0
            self.sock.settimeout(timeout)

            # Receive the response
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")

            # Parse the response
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Ableton")
            self.sock = None
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self.sock = None
            raise Exception(f"Invalid response from Ableton: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self.sock = None
            raise Exception(f"Communication error with Ableton: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("AbletonMCP server starting up")

        # Record startup event for telemetry
        try:
            record_startup()
        except Exception as e:
            logger.debug(f"Failed to record startup telemetry: {e}")

        script_info = None
        try:
            ableton = get_ableton_connection()
            logger.info("Successfully connected to Ableton on startup")
            from .script_handshake import handshake

            script_info = handshake(ableton.send_command)
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
            logger.warning("Make sure the Ableton Remote Script is running")

        if dataset_enabled():
            try:
                recorder = get_recorder()
                if recorder:
                    logger.info(
                        "Dataset recording enabled → Supabase session %s",
                        recorder.session_id,
                    )
                from .script_handshake import script_has_capability
                from .dataset.passive_poller import start_passive_poller

                # Runs even without Live-UI events: it also settles implicit
                # preferences for agent actions.
                start_passive_poller()
                if not script_has_capability("drain_passive_events"):
                    logger.warning(
                        "Passive Live listeners unavailable — Remote Script outdated "
                        "or not loaded. Restart Ableton after script update."
                    )
            except Exception as e:
                logger.warning(f"Failed to start dataset recorder: {e}")
        else:
            reason = "unknown"
            try:
                from .config import telemetry_config  # noqa: F401
                from .telemetry import get_telemetry_consent, is_telemetry_enabled

                if not is_telemetry_enabled():
                    reason = "telemetry disabled (config.enabled=False or DISABLE_TELEMETRY)"
                elif not get_telemetry_consent():
                    reason = "no dataset consent yet — the user has not opted in"
                else:
                    reason = "ABLETON_MCP_DISABLE_DATASET or unexpected gate failure"
            except Exception:
                reason = "MCP_Server/config.py missing or unreadable (gitignored; required for Supabase)"
            logger.info("Dataset recording off — %s", reason)

        yield {"script_info": script_info}
    finally:
        global _ableton_connection
        try:
            from .dataset.passive_poller import stop_passive_poller

            stop_passive_poller()
        except Exception as e:
            logger.debug(f"Failed to stop passive poller: {e}")
        if _ableton_connection:
            logger.info("Disconnecting from Ableton on shutdown")
            _ableton_connection.disconnect()
            _ableton_connection = None
        try:
            recorder = get_recorder()
            if recorder:
                # Bounded wait so queued rows land before the daemon worker
                # is killed at exit.
                recorder.end(timeout=float(
                    os.environ.get("ABLETON_MCP_DATASET_FLUSH_SEC", "5")
                ))
        except Exception as e:
            logger.debug(f"Failed to end dataset session: {e}")
        logger.info("AbletonMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "AbletonMCP",
    lifespan=server_lifespan
)

# Global connection for resources
_ableton_connection = None

def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection

    if _ableton_connection is not None and _ableton_connection.sock is not None:
        try:
            # Check if the socket is still alive by peeking for data
            # MSG_PEEK + MSG_DONTWAIT will raise BlockingIOError if alive but no data,
            # or return b'' if the remote end has closed the connection.
            _ableton_connection.sock.setblocking(False)
            try:
                data = _ableton_connection.sock.recv(1, socket.MSG_PEEK)
                if data == b'':
                    raise ConnectionError("Remote end closed")
            except BlockingIOError:
                pass  # Socket is alive, just no data waiting — this is normal
            finally:
                _ableton_connection.sock.setblocking(True)
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _ableton_connection.disconnect()
            except:
                pass
            _ableton_connection = None
            # Passive events stop arriving while disconnected, so any cached
            # state hash may describe a Live session that has since moved on.
            try:
                recorder = get_recorder()
                if recorder:
                    recorder.invalidate_cached_state()
            except Exception:
                pass
    
    # Connection doesn't exist or is invalid, create a new one
    if _ableton_connection is None:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton at {ABLETON_HOST}:{ABLETON_PORT} (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host=ABLETON_HOST, port=ABLETON_PORT)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    return _ableton_connection
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None

            if attempt < max_attempts:
                import time
                time.sleep(1.0)
        
        # If we get here, all connection attempts failed
        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
    
    return _ableton_connection


# Core Tool endpoints

@mcp.tool()
def set_dataset_consent(ctx: Context, consent: bool, user_said: str = "") -> str:
    """Record the user's answer to the dataset consent question.

    Call this ONLY after the user has answered in their own words. Never infer
    the answer, never call it on their behalf, and never call it because
    contributing seems helpful — consent the user did not give is not consent.
    If they have not been asked yet, ask first and wait for their reply.

    Parameters:
    - consent: True if the user agreed to contribute, False if they declined
    - user_said: The user's reply, quoted as closely as possible
    """
    # Intentionally undecorated: recording this call would mean collecting data
    # from someone who may have just declined.
    try:
        from .dataset.consent import record_consent

        state = record_consent(consent, quote=user_said)
    except Exception as e:
        logger.error(f"Could not record dataset consent: {str(e)}")
        return f"Error recording consent: {str(e)}"

    if not consent:
        return (
            "Recorded: dataset contribution declined. Nothing is uploaded, and "
            "you will not be asked again."
        )

    try:
        from .telemetry import refresh_consent_from_dataset

        refresh_consent_from_dataset()
        recorder = get_recorder()
        # Under opt-in the poller is almost never started at boot — consent did
        # not exist yet — so a grant has to start it here or passive Live events
        # would be missed for the rest of the session. Idempotent and
        # self-gating, so this is safe even if startup did start it.
        from .dataset.passive_poller import start_passive_poller

        start_passive_poller()
    except Exception as e:
        logger.error(f"Consent saved but recording failed to start: {str(e)}")
        return (
            "Consent saved, but recording could not start in this session. "
            "It will begin after a restart."
        )

    if recorder is None:
        return (
            "Consent saved, but recording is unavailable (telemetry may be "
            f"disabled). State: {state}."
        )
    return (
        "Thank you — this session is now being contributed to the open dataset. "
        "Say so at any time to stop, or set ABLETON_MCP_DISABLE_DATASET=1."
    )


@mcp.tool()
@telemetry_tool("get_session_info")
@trajectory_tool("get_session_info")
def get_session_info(ctx: Context, user_prompt: str = "") -> str:
    """Get detailed information about the current Ableton session

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"


@mcp.tool()
@telemetry_tool("get_remote_script_info")
@trajectory_tool("get_remote_script_info", modifying=False)
def get_remote_script_info(ctx: Context, user_prompt: str = "") -> str:
    """
    Report Ableton Remote Script version and capabilities (handshake).

    Use this to verify the Live-side bridge matches this MCP server package.
    """
    try:
        from .remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION
        from .script_handshake import get_cached_script_info, handshake

        ableton = get_ableton_connection()
        info = handshake(ableton.send_command)
        cached = dict(get_cached_script_info() or info)
        cached["expected_version"] = EXPECTED_REMOTE_SCRIPT_VERSION
        return json.dumps(cached, indent=2)
    except Exception as e:
        logger.error(f"Error getting remote script info: {str(e)}")
        return f"Error getting remote script info: {str(e)}"

@mcp.tool()
@telemetry_tool("get_track_info")
@trajectory_tool("get_track_info")
def get_track_info(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Get detailed information about a specific track in Ableton.

    Parameters:
    - track_index: The index of the track to get information about
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_info", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"


@mcp.tool()
@telemetry_tool("get_clip_notes")
@trajectory_tool("get_clip_notes")
def get_clip_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    user_prompt: str = "",
) -> str:
    """
    Read all MIDI notes from a Session-view clip.

    Returns pitch, start_time, duration, velocity, mute (and extended fields when available).

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot index
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("get_clip_notes")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "get_clip_notes",
            {"track_index": track_index, "clip_index": clip_index},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting clip notes: {str(e)}")
        return f"Error getting clip notes: {str(e)}"


@mcp.tool()
@telemetry_tool("get_device_parameters")
@trajectory_tool("get_device_parameters")
def get_device_parameters(
    ctx: Context,
    track_index: int,
    device_index: int,
    user_prompt: str = "",
) -> str:
    """
    Read all parameters for a device on a track (name, value, min, max).

    Parameters:
    - track_index: Track that owns the device
    - device_index: Index into the track's device chain
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "get_device_parameters",
            {"track_index": track_index, "device_index": device_index},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting device parameters: {str(e)}")
        return f"Error getting device parameters: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_device_parameter")
@trajectory_tool("set_device_parameter")
def set_device_parameter(
    ctx: Context,
    track_index: int,
    device_index: int,
    parameter_index: int,
    value: float,
    user_prompt: str = "",
) -> str:
    """
    Set a device parameter to a specific value.

    Use get_device_parameters first to discover parameter indices and ranges.

    Parameters:
    - track_index: Track that owns the device
    - device_index: Index into the track's device chain
    - parameter_index: Index into device.parameters
    - value: New parameter value (Live parameter units)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "set_device_parameter",
            {
                "track_index": track_index,
                "device_index": device_index,
                "parameter_index": parameter_index,
                "value": value,
            },
        )
        return (
            f"Set {result.get('name', 'parameter')} "
            f"{result.get('old_value')} → {result.get('value')}"
        )
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"


@mcp.tool()
@telemetry_tool("get_session_snapshot")
@trajectory_tool("get_session_snapshot")
def get_session_snapshot(
    ctx: Context,
    include_notes: bool = True,
    include_params: bool = True,
    user_prompt: str = "",
) -> str:
    """
    Capture a full project state snapshot for trajectory recording.

    Includes session metadata, every track (mixer, devices, session clips,
    arrangement clips), optional MIDI notes, and optional device parameters.
    Used to version musical state S_t → S_{t+1}.

    Parameters:
    - include_notes: Include MIDI note arrays in clips (default True)
    - include_params: Include device parameter values (default True)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("get_session_snapshot")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "get_session_snapshot",
            {
                "include_notes": include_notes,
                "include_params": include_params,
            },
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session snapshot: {str(e)}")
        return f"Error getting session snapshot: {str(e)}"


@mcp.tool()
@telemetry_tool("create_midi_track")
@trajectory_tool("create_midi_track")
def create_midi_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return f"Created new MIDI track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
@telemetry_tool("create_audio_track")
@trajectory_tool("create_audio_track")
def create_audio_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new audio track in the Ableton session.

    Use this for recorded or imported audio (samples, stems, vocals). For MIDI
    instruments use create_midi_track instead.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_track", {"index": index})
        return f"Created new audio track: {result.get('name', 'unknown')}"
    except Exception as e:
        logger.error(f"Error creating audio track: {str(e)}")
        return f"Error creating audio track: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_track_name")
@trajectory_tool("set_track_name")
def set_track_name(ctx: Context, track_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_clip")
@trajectory_tool("create_clip")
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0, user_prompt: str = "") -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_clip", {
            "track_index": track_index, 
            "clip_index": clip_index, 
            "length": length
        })
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_audio_clip")
@trajectory_tool("create_audio_clip")
def create_audio_clip(ctx: Context, track_index: int, clip_index: int, path: str, user_prompt: str = "") -> str:
    """
    Create a new audio clip in an audio track's clip slot by importing a file.

    Requires Ableton Live 12.0.5 or newer — the underlying
    ClipSlot.create_audio_clip Live API was introduced in 12.0.5 and is not
    available in earlier 12.0.x releases.

    Parameters:
    - track_index: The index of the audio track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - path: Absolute path to a supported audio file (e.g. a .wav). The target
      track must be an audio track and the clip slot must be empty.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        })
        return f"Created audio clip '{result.get('name', 'clip')}' at track {track_index}, slot {clip_index} (length {result.get('length', '?')} beats)"
    except Exception as e:
        logger.error(f"Error creating audio clip: {str(e)}")
        return f"Error creating audio clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("add_notes_to_clip", capture_notes=True)
@trajectory_tool("add_notes_to_clip")
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]],
    user_prompt: str = ""
) -> str:
    """
    Add MIDI notes to a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("add_notes_to_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes
        })
        return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        return f"Error adding notes to clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("clear_notes_from_clip")
def clear_notes_from_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    user_prompt: str = ""
) -> str:
    """
    Remove all MIDI notes from a Session clip.

    Writes are additive (add_notes_to_clip only appends), so to truly *modify*
    a clip you clear it first, then add the new notes. Use this with
    get_clip_notes and add_notes_to_clip for a real read -> modify -> write
    loop: read the notes, edit the list, clear_notes_from_clip, then
    add_notes_to_clip the edited notes.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("clear_notes_from_clip")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command("clear_notes_from_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return "Cleared {n} note(s) from clip '{name}' (track {t}, slot {c})".format(
            n=result.get("cleared_count", "?"),
            name=result.get("clip_name", "clip"),
            t=track_index,
            c=clip_index,
        )
    except Exception as e:
        logger.error(f"Error clearing notes from clip: {str(e)}")
        return f"Error clearing notes from clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_clip_name")
@trajectory_tool("set_clip_name")
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_arrangement_clip_name")
@trajectory_tool("set_arrangement_clip_name")
def set_arrangement_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip placed in the Arrangement timeline.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip within track.arrangement_clips, in the
      same order returned by get_arrangement_clips (i.e. ordered by start_time)
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_arrangement_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed arrangement clip at track {track_index}, index {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting arrangement clip name: {str(e)}")
        return f"Error setting arrangement clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_tempo")
@trajectory_tool("set_tempo")
def set_tempo(ctx: Context, tempo: float, user_prompt: str = "") -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("load_instrument_or_effect")
@trajectory_tool("load_instrument_or_effect")
def load_instrument_or_effect(ctx: Context, track_index: int, uri: str, user_prompt: str = "") -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Parameters:
    - track_index: The index of the track to load the instrument on
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": uri
        })
        
        # Check if the instrument was loaded successfully
        if result.get("loaded", False):
            new_devices = result.get("new_devices", [])
            if new_devices:
                return f"Loaded instrument with URI '{uri}' on track {track_index}. New devices: {', '.join(new_devices)}"
            else:
                devices = result.get("devices_after", [])
                return f"Loaded instrument with URI '{uri}' on track {track_index}. Devices on track: {', '.join(devices)}"
        else:
            return f"Failed to load instrument with URI '{uri}'"
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"

@mcp.tool()
@telemetry_tool("fire_clip")
@trajectory_tool("fire_clip")
def fire_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_clip")
@trajectory_tool("stop_clip")
def stop_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_clip")
def delete_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Delete the clip in the given clip slot, freeing it for reuse.

    Use this before create_clip when you want to overwrite an existing clip
    (create_clip itself refuses to write into an occupied slot).

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot to clear
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("delete_clip")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error deleting clip: {str(e)}")
        return f"Error deleting clip: {str(e)}"


@mcp.tool()
@telemetry_tool("start_playback")
@trajectory_tool("start_playback")
def start_playback(ctx: Context, user_prompt: str = "") -> str:
    """Start playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_playback")
@trajectory_tool("stop_playback")
def stop_playback(ctx: Context, user_prompt: str = "") -> str:
    """Stop playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"


# Recording workflow. Values are Live parameter units (volume and sends are
# typically 0.0–1.0, pan typically -1.0–1.0), not dB. None of these wait for
# audio to finish recording. Keep MAX_EMPTY_SLOT_RECORD_BEATS in sync with
# AbletonMCP_Remote_Script.
_MONITORING_NAMES = {"in", "auto", "off"}
_STOP_SCOPES = {"arrangement", "session", "slot", "recording"}
_CLIP_VIEWS = {"session", "arrangement"}
_MAX_EMPTY_SLOT_RECORD_BEATS = 16384


def _omit_none(params: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


def _recording_call(command: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Send one recording-workflow command and return JSON or an error string."""
    from .script_handshake import require_capability

    missing = require_capability(command)
    if missing:
        return missing
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(command, params or {})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error in {command}: {e}")
        return f"Error {command}: {e}"


def _reject_record_length(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "record_length must be a positive finite number of beats"
    if (
        value != value
        or value <= 0
        or value == float("inf")
        or value == float("-inf")
        or value > _MAX_EMPTY_SLOT_RECORD_BEATS
    ):
        return (
            "record_length must be a positive finite number of beats "
            f"up to {_MAX_EMPTY_SLOT_RECORD_BEATS}"
        )
    return None


@mcp.tool()
@telemetry_tool("get_recording_state")
@trajectory_tool("get_recording_state", modifying=False)
def get_recording_state(ctx: Context, user_prompt: str = "") -> str:
    """Read arrangement record, session record, overdub, count-in, and which slots are recording.

    Count-in is reported and cannot be changed here. This does not start or stop anything.
    """
    return _recording_call("get_recording_state", {})


@mcp.tool()
@telemetry_tool("get_track_mixer_state")
@trajectory_tool("get_track_mixer_state", modifying=False)
def get_track_mixer_state(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """Read arm, mute, solo, volume, pan, sends, monitoring, and input routing for one track.

    Includes the input routing names Live currently offers, so a later
    set_track_input_routing or prepare_track can use one of those strings.
    """
    return _recording_call("get_track_mixer_state", {"track_index": track_index})


@mcp.tool()
@telemetry_tool("set_track_arm")
@trajectory_tool("set_track_arm")
def set_track_arm(
    ctx: Context,
    track_index: int,
    armed: Optional[bool] = None,
    arm: Optional[bool] = None,
    user_prompt: str = "",
) -> str:
    """Set a track's arm button explicitly. This is not a toggle.

    Pass armed or arm. Group, return, and main tracks cannot be armed.
    When Live's exclusive-arm preference is on, arming one track disarms
    the others; that preference is not changed.
    """
    if armed is None and arm is None:
        return "Error set_track_arm: armed or arm is required"
    if armed is not None and arm is not None and armed != arm:
        return "Error set_track_arm: conflicting armed and arm"
    params = {"track_index": track_index}
    if armed is not None:
        params["armed"] = armed
    if arm is not None:
        params["arm"] = arm
    return _recording_call("set_track_arm", params)


@mcp.tool()
@telemetry_tool("set_track_mute")
@trajectory_tool("set_track_mute")
def set_track_mute(ctx: Context, track_index: int, mute: bool, user_prompt: str = "") -> str:
    """Set a track's mute button explicitly. This is not a toggle."""
    return _recording_call("set_track_mute", {"track_index": track_index, "mute": mute})


@mcp.tool()
@telemetry_tool("set_track_solo")
@trajectory_tool("set_track_solo")
def set_track_solo(ctx: Context, track_index: int, solo: bool, user_prompt: str = "") -> str:
    """Set a track's solo button explicitly. This is not a toggle.

    Live does not apply exclusive-solo when solo is set from the API, so other
    solos are left as they are.
    """
    return _recording_call("set_track_solo", {"track_index": track_index, "solo": solo})


@mcp.tool()
@telemetry_tool("set_track_volume")
@trajectory_tool("set_track_volume")
def set_track_volume(
    ctx: Context,
    track_index: int,
    value: Optional[float] = None,
    volume: Optional[float] = None,
    user_prompt: str = "",
) -> str:
    """Set mixer volume in Live's raw parameter units (usually 0.0 to 1.0, not dB).

    Pass value or volume. Values outside the parameter's own min/max are rejected
    and the volume is left unchanged.
    """
    if value is None and volume is None:
        return "Error set_track_volume: value or volume is required"
    if value is not None and volume is not None and value != volume:
        return "Error set_track_volume: conflicting value and volume"
    params = {"track_index": track_index}
    if value is not None:
        params["value"] = value
    if volume is not None:
        params["volume"] = volume
    return _recording_call("set_track_volume", params)


@mcp.tool()
@telemetry_tool("set_track_panning")
@trajectory_tool("set_track_panning")
def set_track_panning(
    ctx: Context,
    track_index: int,
    value: Optional[float] = None,
    panning: Optional[float] = None,
    pan: Optional[float] = None,
    user_prompt: str = "",
) -> str:
    """Set mixer pan in Live's raw parameter units (usually -1.0 left to 1.0 right).

    Pass value, panning, or pan.
    """
    sent = [item for item in (value, panning, pan) if item is not None]
    if not sent:
        return "Error set_track_panning: value, panning, or pan is required"
    if any(item != sent[0] for item in sent[1:]):
        return "Error set_track_panning: conflicting pan values"
    params = {"track_index": track_index}
    if value is not None:
        params["value"] = value
    if panning is not None:
        params["panning"] = panning
    if pan is not None:
        params["pan"] = pan
    return _recording_call("set_track_panning", params)


@mcp.tool()
@telemetry_tool("set_track_send")
@trajectory_tool("set_track_send")
def set_track_send(
    ctx: Context,
    track_index: int,
    send_index: int,
    value: float,
    user_prompt: str = "",
) -> str:
    """Set one send in Live's raw parameter units (usually 0.0 to 1.0).

    send_index is the position in the track's send list. An out-of-range index does not change any send.
    """
    return _recording_call(
        "set_track_send",
        {"track_index": track_index, "send_index": send_index, "value": value},
    )


@mcp.tool()
@telemetry_tool("set_track_monitoring")
@trajectory_tool("set_track_monitoring")
def set_track_monitoring(
    ctx: Context, track_index: int, monitoring: str, user_prompt: str = ""
) -> str:
    """Set input monitoring to in, auto, or off.

    Live stores this as 0=in, 1=auto, 2=off. The response includes that integer.
    """
    if not isinstance(monitoring, str) or monitoring.strip().lower() not in _MONITORING_NAMES:
        return "Error set_track_monitoring: monitoring must be one of in, auto, off"
    return _recording_call(
        "set_track_monitoring",
        {"track_index": track_index, "monitoring": monitoring.strip().lower()},
    )


@mcp.tool()
@telemetry_tool("set_track_input_routing")
@trajectory_tool("set_track_input_routing")
def set_track_input_routing(
    ctx: Context,
    track_index: int,
    input_routing_type: str = "",
    input_routing_channel: str = "",
    user_prompt: str = "",
) -> str:
    """Set input routing by the display names Live reports for this track.

    Pass either or both. Names must match get_track_mixer_state's available
    lists (exact match, or a unique case-insensitive match). Output routing is
    not changed. A channel that is not valid for the new type leaves the previous type in place.
    """
    params: Dict[str, Any] = {"track_index": track_index}
    if input_routing_type:
        params["input_routing_type"] = input_routing_type
    if input_routing_channel:
        params["input_routing_channel"] = input_routing_channel
    if len(params) == 1:
        return (
            "Error set_track_input_routing: input_routing_type or "
            "input_routing_channel is required"
        )
    return _recording_call("set_track_input_routing", params)


@mcp.tool()
@telemetry_tool("set_arrangement_record")
@trajectory_tool("set_arrangement_record")
def set_arrangement_record(
    ctx: Context,
    enabled: Optional[bool] = None,
    on: Optional[bool] = None,
    allow_overwrite: bool = False,
    user_prompt: str = "",
) -> str:
    """Set the arrangement record button explicitly. This does not start or stop playback.

    Pass enabled or on. Enabling is refused when an armed track has an
    arrangement clip under the playhead, unless allow_overwrite is true.
    Turning it off keeps the recorded arrangement audio and does not delete anything.
    """
    if enabled is None and on is None:
        return "Error set_arrangement_record: enabled or on is required"
    if enabled is not None and on is not None and enabled != on:
        return "Error set_arrangement_record: conflicting enabled and on"
    params = {"allow_overwrite": allow_overwrite}
    if enabled is not None:
        params["enabled"] = enabled
    if on is not None:
        params["on"] = on
    return _recording_call("set_arrangement_record", params)


@mcp.tool()
@telemetry_tool("set_session_record")
@trajectory_tool("set_session_record")
def set_session_record(
    ctx: Context,
    enabled: Optional[bool] = None,
    on: Optional[bool] = None,
    allow_overwrite: bool = False,
    user_prompt: str = "",
) -> str:
    """Set the session record button explicitly. This is not Song.trigger_session_record.

    Enabling records into the selected scene on every armed track and can
    overdub a clip that is already playing. That is refused unless
    allow_overwrite is true. To record one empty slot, use record_empty_slot
    instead. Turning this off does not delete clips and does not stop playback.
    """
    if enabled is None and on is None:
        return "Error set_session_record: enabled or on is required"
    if enabled is not None and on is not None and enabled != on:
        return "Error set_session_record: conflicting enabled and on"
    params = {"allow_overwrite": allow_overwrite}
    if enabled is not None:
        params["enabled"] = enabled
    if on is not None:
        params["on"] = on
    return _recording_call("set_session_record", params)


@mcp.tool()
@telemetry_tool("set_arrangement_overdub")
@trajectory_tool("set_arrangement_overdub")
def set_arrangement_overdub(
    ctx: Context,
    enabled: Optional[bool] = None,
    on: Optional[bool] = None,
    user_prompt: str = "",
) -> str:
    """Set MIDI arrangement overdub explicitly. Pass enabled or on.

    This does not start recording. Song.overdub is a legacy alias of session
    record and is not set by this tool.
    """
    if enabled is None and on is None:
        return "Error set_arrangement_overdub: enabled or on is required"
    if enabled is not None and on is not None and enabled != on:
        return "Error set_arrangement_overdub: conflicting enabled and on"
    params = {}
    if enabled is not None:
        params["enabled"] = enabled
    if on is not None:
        params["on"] = on
    return _recording_call("set_arrangement_overdub", params)


@mcp.tool()
@telemetry_tool("record_empty_slot")
@trajectory_tool("record_empty_slot")
def record_empty_slot(
    ctx: Context,
    track_index: int,
    clip_index: int,
    arm: bool = True,
    record_length: Optional[float] = None,
    user_prompt: str = "",
) -> str:
    """Record into one empty Session slot by arming that track, if requested, and firing the slot.

    Refuses when the slot already has a clip or is recording. Does not turn on
    the global session record button, so other tracks are not punched in by
    this call. Omit record_length to keep recording until stop_recording.
    Pass record_length in beats for a fixed length; Live then ends the take
    itself. The response means the slot was fired, not that audio has arrived.
    Launch quantization can delay the punch. Count-in is not changed.
    """
    length_error = _reject_record_length(record_length)
    if length_error:
        return f"Error record_empty_slot: {length_error}"
    params: Dict[str, Any] = {
        "track_index": track_index,
        "clip_index": clip_index,
        "arm": arm,
    }
    if record_length is not None:
        params["record_length"] = record_length
    return _recording_call("record_empty_slot", params)


@mcp.tool()
@telemetry_tool("stop_recording")
@trajectory_tool("stop_recording")
def stop_recording(
    ctx: Context,
    scope: str,
    track_index: Optional[int] = None,
    clip_index: Optional[int] = None,
    user_prompt: str = "",
) -> str:
    """Stop recording without deleting clips and without stopping the transport.

    scope:
    - arrangement: arrangement record button off. Playback continues.
    - session: session record button off. Playback continues.
    - slot: stop session clips on that track only when that slot is recording.
      Requires track_index and clip_index. A slot that is merely playing is left playing.
    - recording: both record buttons off, and session clips stopped only on
      tracks that are actually recording.
    """
    if not isinstance(scope, str) or scope not in _STOP_SCOPES:
        return (
            "Error stop_recording: scope must be one of "
            "arrangement, session, slot, recording"
        )
    params: Dict[str, Any] = {"scope": scope}
    if scope == "slot":
        if track_index is None or clip_index is None:
            return "Error stop_recording: slot scope requires track_index and clip_index"
        params["track_index"] = track_index
        params["clip_index"] = clip_index
    elif track_index is not None or clip_index is not None:
        return "Error stop_recording: track_index and clip_index are only valid when scope is slot"
    return _recording_call("stop_recording", params)


@mcp.tool()
@telemetry_tool("prepare_track")
@trajectory_tool("prepare_track")
def prepare_track(
    ctx: Context,
    track_index: int,
    name: Optional[str] = None,
    arm: Optional[bool] = None,
    mute: Optional[bool] = None,
    solo: Optional[bool] = None,
    volume: Optional[float] = None,
    panning: Optional[float] = None,
    sends: Optional[List[Dict[str, float]]] = None,
    monitoring: Optional[str] = None,
    input_routing_type: Optional[str] = None,
    input_routing_channel: Optional[str] = None,
    user_prompt: str = "",
) -> str:
    """Set any mix of name, arm, mute, solo, volume, pan, sends, monitoring, and input routing in one call.

    Omitted arguments are left unchanged. Invalid values are rejected before Live is changed.
    This does not start recording. sends is a list of {"index": int, "value": float}.
    monitoring is in, auto, or off. Volume, pan, and sends use Live parameter units.
    """
    if monitoring is not None and (
        not isinstance(monitoring, str) or monitoring.strip().lower() not in _MONITORING_NAMES
    ):
        return "Error prepare_track: monitoring must be one of in, auto, off"
    params = _omit_none({
        "track_index": track_index,
        "name": name,
        "arm": arm,
        "mute": mute,
        "solo": solo,
        "volume": volume,
        "panning": panning,
        "sends": sends,
        "monitoring": None if monitoring is None else monitoring.strip().lower(),
        "input_routing_type": input_routing_type,
        "input_routing_channel": input_routing_channel,
    })
    return _recording_call("prepare_track", params)


@mcp.tool()
@telemetry_tool("get_clip_region")
@trajectory_tool("get_clip_region", modifying=False)
def get_clip_region(
    ctx: Context,
    track_index: int,
    clip_index: int,
    view: str = "session",
    user_prompt: str = "",
) -> str:
    """Read a clip's start marker, end marker, loop, and whether it is recording.

    view is session or arrangement. Markers are beats for MIDI and warped audio,
    and seconds for unwarped audio. This does not crop the clip or read the sample out.
    """
    if view not in _CLIP_VIEWS:
        return "Error get_clip_region: view must be session or arrangement"
    return _recording_call(
        "get_clip_region",
        {"track_index": track_index, "clip_index": clip_index, "view": view},
    )


@mcp.tool()
@telemetry_tool("set_clip_region")
@trajectory_tool("set_clip_region")
def set_clip_region(
    ctx: Context,
    track_index: int,
    clip_index: int,
    view: str = "session",
    start_marker: Optional[float] = None,
    end_marker: Optional[float] = None,
    loop_start: Optional[float] = None,
    loop_end: Optional[float] = None,
    looping: Optional[bool] = None,
    user_prompt: str = "",
) -> str:
    """Move a clip's start/end markers or loop. This does not crop or replace the source file.

    end_marker must stay greater than start_marker. If you send only markers,
    the existing loop is clamped inside them. An explicit loop that falls
    outside the markers is rejected and nothing is written. This does not crop.
    Unwarped audio cannot be set to looping. A clip that is recording is left
    unchanged. view is session or arrangement.
    """
    if view not in _CLIP_VIEWS:
        return "Error set_clip_region: view must be session or arrangement"
    params = _omit_none({
        "track_index": track_index,
        "clip_index": clip_index,
        "view": view,
        "start_marker": start_marker,
        "end_marker": end_marker,
        "loop_start": loop_start,
        "loop_end": loop_end,
        "looping": looping,
    })
    if not any(
        key in params
        for key in ("start_marker", "end_marker", "loop_start", "loop_end", "looping")
    ):
        return (
            "Error set_clip_region: set at least one of start_marker, end_marker, "
            "loop_start, loop_end, looping"
        )
    return _recording_call("set_clip_region", params)


@mcp.tool()
@telemetry_tool("quick_record")
@trajectory_tool("quick_record")
def quick_record(
    ctx: Context,
    track_index: int,
    beats: Optional[float] = None,
    bars: Optional[float] = None,
    slot_index: Optional[int] = None,
    arm: bool = True,
    monitoring: Optional[str] = None,
    input_routing_type: Optional[str] = None,
    input_routing_channel: Optional[str] = None,
    disarm_after: bool = False,
    pre_roll_bars: Optional[float] = None,
    create_scene_if_full: bool = False,
    user_prompt: str = "",
) -> str:
    """Arm if needed, pick an empty Session slot, and fire it for a fixed length.

    Pass bars or beats, not both. slot_index defaults to the first empty slot.
    This fire uses launch quantization none so the length is exact; the song's
    quantization setting is not changed. Monitoring defaults to auto; pass
    monitoring='keep' to leave it. disarm_after arms until the take stops
    recording, not before. pre_roll_bars starts the metronome and records
    after that many bars without blocking. create_scene_if_full appends a
    scene when every slot is full.
    """
    if monitoring is not None and (
        not isinstance(monitoring, str)
        or monitoring.strip().lower() not in (_MONITORING_NAMES | {"keep"})
    ):
        return "Error quick_record: monitoring must be auto, in, off, or keep"
    if (beats is None) == (bars is None):
        return "Error quick_record: pass bars or beats, not both and not neither"
    length_error = _reject_record_length(beats) if beats is not None else None
    if length_error:
        return f"Error quick_record: {length_error}"
    if bars is not None and (isinstance(bars, bool) or not isinstance(bars, (int, float)) or bars <= 0):
        return "Error quick_record: bars must be a positive number"
    params = _omit_none({
        "track_index": track_index,
        "beats": beats,
        "bars": bars,
        "slot_index": slot_index,
        "arm": arm,
        "monitoring": None if monitoring is None else monitoring.strip().lower(),
        "input_routing_type": input_routing_type,
        "input_routing_channel": input_routing_channel,
        "disarm_after": disarm_after,
        "pre_roll_bars": pre_roll_bars,
        "create_scene_if_full": create_scene_if_full,
    })
    return _recording_call("quick_record", params)


@mcp.tool()
@telemetry_tool("quick_arrangement_record")
@trajectory_tool("quick_arrangement_record")
def quick_arrangement_record(
    ctx: Context,
    track_indexes: List[int],
    start_time: Optional[float] = None,
    arm: bool = True,
    allow_overwrite: bool = False,
    user_prompt: str = "",
) -> str:
    """Arm the tracks, seek, enable arrangement record, and start playback in one call.

    Refuses, with no writes, when a listed track has an arrangement clip covering
    the record position, unless allow_overwrite is true. Launch quantization is not changed.
    """
    if not isinstance(track_indexes, list) or not track_indexes:
        return "Error quick_arrangement_record: track_indexes must be a non-empty list"
    params = {
        "track_indexes": track_indexes,
        "arm": arm,
        "allow_overwrite": allow_overwrite,
    }
    if start_time is not None:
        params["start_time"] = start_time
    return _recording_call("quick_arrangement_record", params)


@mcp.tool()
@telemetry_tool("quick_arrangement_stop")
@trajectory_tool("quick_arrangement_stop")
def quick_arrangement_stop(
    ctx: Context,
    track_indexes: Optional[List[int]] = None,
    disarm: bool = False,
    user_prompt: str = "",
) -> str:
    """Stop playback, turn arrangement record off, and optionally disarm.

    Clips are not deleted. With disarm and no track_indexes, every currently
    armed track is disarmed. playing may be pending if Live has not settled yet.
    """
    params = {"disarm": disarm}
    if track_indexes is not None:
        params["track_indexes"] = track_indexes
    return _recording_call("quick_arrangement_stop", params)


@mcp.tool()
@telemetry_tool("set_mixer_batch")
@trajectory_tool("set_mixer_batch")
def set_mixer_batch(
    ctx: Context,
    tracks: List[Dict[str, Any]],
    user_prompt: str = "",
) -> str:
    """Set volume, pan, mute, solo, arm, and sends for several tracks in one call.

    Each item needs track_index and any of volume (or value), pan (or panning),
    mute, solo, arm (or armed), and sends. One invalid item changes nothing.
    Results are per track.
    """
    if not isinstance(tracks, list):
        return "Error set_mixer_batch: tracks must be a list"
    return _recording_call("set_mixer_batch", {"tracks": tracks})


@mcp.tool()
@telemetry_tool("record_loop")
@trajectory_tool("record_loop")
def record_loop(
    ctx: Context,
    track_index: int,
    bars: float,
    max_takes: Optional[int] = None,
    monitoring: Optional[str] = None,
    pre_roll_bars: Optional[float] = None,
    user_prompt: str = "",
) -> str:
    """Chain fixed-length takes on one track without blocking Live's main thread.

    Each take is bars long. The next empty slot is fired when the current take
    stops recording. A scene is created when the track has no empty slot.
    Without max_takes the loop stops at 128 takes or at stop_record_loop.
    Monitoring defaults to auto.
    """
    if monitoring is not None and (
        not isinstance(monitoring, str)
        or monitoring.strip().lower() not in (_MONITORING_NAMES | {"keep"})
    ):
        return "Error record_loop: monitoring must be auto, in, off, or keep"
    params = _omit_none({
        "track_index": track_index,
        "bars": bars,
        "max_takes": max_takes,
        "monitoring": None if monitoring is None else monitoring.strip().lower(),
        "pre_roll_bars": pre_roll_bars,
    })
    return _recording_call("record_loop", params)


@mcp.tool()
@telemetry_tool("stop_record_loop")
@trajectory_tool("stop_record_loop")
def stop_record_loop(
    ctx: Context,
    track_index: int,
    discard_current: bool = True,
    user_prompt: str = "",
) -> str:
    """Stop a record_loop immediately.

    discard_current deletes only the clip that is recording when this call
    arrives, so a spoken stop does not remain in the take. Finished takes stay.
    """
    return _recording_call("stop_record_loop", {
        "track_index": track_index,
        "discard_current": discard_current,
    })


@mcp.tool()
@telemetry_tool("stop_take")
@trajectory_tool("stop_take")
def stop_take(
    ctx: Context,
    track_index: Optional[int] = None,
    discard_current: bool = True,
    user_prompt: str = "",
) -> str:
    """Stop the clip that is recording now.

    discard_current defaults to true and deletes only that clip. Pass false
    to keep it. Other clips are not deleted.
    """
    params = {"discard_current": discard_current}
    if track_index is not None:
        params["track_index"] = track_index
    return _recording_call("stop_take", params)


@mcp.tool()
@telemetry_tool("set_metronome")
@trajectory_tool("set_metronome")
def set_metronome(
    ctx: Context,
    enabled: Optional[bool] = None,
    on: Optional[bool] = None,
    user_prompt: str = "",
) -> str:
    """Set Song.metronome explicitly. Pass enabled or on. This does not start playback."""
    if enabled is None and on is None:
        return "Error set_metronome: enabled or on is required"
    if enabled is not None and on is not None and enabled != on:
        return "Error set_metronome: conflicting enabled and on"
    params = {}
    if enabled is not None:
        params["enabled"] = enabled
    if on is not None:
        params["on"] = on
    return _recording_call("set_metronome", params)


@mcp.tool()
@telemetry_tool("set_count_in")
@trajectory_tool("set_count_in")
def set_count_in(ctx: Context, bars: int, user_prompt: str = "") -> str:
    """Try to set the metronome count-in to 0, 1, 2, or 4 bars.

    The Live 12.4.5 object model lists Song.count_in_duration as get/observe.
    If Live refuses the write, the result says count_in_writable false. Use
    pre_roll_bars on quick_record or record_loop for a timed start instead.
    """
    return _recording_call("set_count_in", {"bars": bars})


@mcp.tool()
@telemetry_tool("create_scene")
@trajectory_tool("create_scene")
def create_scene(
    ctx: Context, index: int = -1, user_prompt: str = ""
) -> str:
    """Create a Session scene. index -1 appends. Each track gains a clip slot."""
    return _recording_call("create_scene", {"index": index})


@mcp.tool()
@telemetry_tool("keep_take")
@trajectory_tool("keep_take")
def keep_take(
    ctx: Context,
    track_index: int,
    take: Optional[int] = None,
    clip_index: Optional[int] = None,
    user_prompt: str = "",
) -> str:
    """Mark one finished take. take is 1-based among clips on the track ('keep 3').

    Appends ' KEEP' to the clip name and tries to set a green clip color.
    A clip that is still recording is not renamed.
    """
    if take is None and clip_index is None:
        return "Error keep_take: take or clip_index is required"
    params = {"track_index": track_index}
    if take is not None:
        params["take"] = take
    if clip_index is not None:
        params["clip_index"] = clip_index
    return _recording_call("keep_take", params)


@mcp.tool()
@telemetry_tool("list_takes")
@trajectory_tool("list_takes", modifying=False)
def list_takes(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """List clips on a track with 1-based take numbers, names, and lengths."""
    return _recording_call("list_takes", {"track_index": track_index})


@mcp.tool()
@telemetry_tool("get_performance_state")
@trajectory_tool("get_performance_state", modifying=False)
def get_performance_state(ctx: Context, user_prompt: str = "") -> str:
    """Lightweight state for the voice trigger: view, armed tracks, recording, last take, tempo, metronome."""
    return _recording_call("get_performance_state", {})


@mcp.tool()
@telemetry_tool("show_view")
@trajectory_tool("show_view")
def show_view(ctx: Context, view: str, user_prompt: str = "") -> str:
    """Show and focus Session or Arrangement."""
    return _recording_call("show_view", {"view": view})


@mcp.tool()
@telemetry_tool("arm_tracks")
@trajectory_tool("arm_tracks")
def arm_tracks(
    ctx: Context,
    names: List[str],
    exclusive: bool = True,
    user_prompt: str = "",
) -> str:
    """Arm tracks by name. Exclusive disarms every other track. Monitoring becomes auto."""
    return _recording_call("arm_tracks", {"names": names, "exclusive": exclusive})


@mcp.tool()
@telemetry_tool("take")
@trajectory_tool("take")
def take(
    ctx: Context,
    bars: float = 8,
    loop: bool = False,
    count_in: bool = True,
    names: Optional[List[str]] = None,
    view: Optional[str] = None,
    user_prompt: str = "",
) -> str:
    """Record one take on every armed track. Default 8 bars and 1-bar count-in.

    Session uses the next empty scene row. Arrangement uses the loop brace
    from the playhead, or from the loop start when the loop is already on.
    loop=true leaves that brace looping so Live can open a take lane per pass.
    """
    params = {"bars": bars, "loop": loop, "count_in": count_in}
    if names is not None:
        params["names"] = names
    if view is not None:
        params["view"] = view
    return _recording_call("take", params)


@mcp.tool()
@telemetry_tool("again")
@trajectory_tool("again")
def again(ctx: Context, user_prompt: str = "") -> str:
    """Delete the last take and start another with the same defaults."""
    return _recording_call("again", {})


@mcp.tool()
@telemetry_tool("scrap")
@trajectory_tool("scrap")
def scrap(ctx: Context, user_prompt: str = "") -> str:
    """Delete the last finished take. Does not delete a take that is still recording."""
    return _recording_call("scrap", {})


@mcp.tool()
@telemetry_tool("cancel")
@trajectory_tool("cancel")
def cancel(ctx: Context, user_prompt: str = "") -> str:
    """Stop transport and recording, and delete only the take that is recording now."""
    return _recording_call("cancel", {})


@mcp.tool()
@telemetry_tool("play_transport")
@trajectory_tool("play_transport")
def play_transport(ctx: Context, user_prompt: str = "") -> str:
    """Start or continue playback with record off. Does not delete takes."""
    return _recording_call("play_transport", {})


@mcp.tool()
@telemetry_tool("pause_transport")
@trajectory_tool("pause_transport")
def pause_transport(ctx: Context, user_prompt: str = "") -> str:
    """Stop the transport without deleting takes and with record off."""
    return _recording_call("pause_transport", {})


@mcp.tool()
@telemetry_tool("nudge_tempo")
@trajectory_tool("nudge_tempo")
def nudge_tempo(ctx: Context, delta: float = 5, user_prompt: str = "") -> str:
    """Add delta BPM to the tempo. Faster is +5, slower is -5. Clamped to 20-999."""
    return _recording_call("nudge_tempo", {"delta": delta})


@mcp.tool()
@telemetry_tool("mark_playhead")
@trajectory_tool("mark_playhead")
def mark_playhead(ctx: Context, name: str = "", user_prompt: str = "") -> str:
    """Add or rename a locator at the current playhead. Does not move the playhead."""
    params = {}
    if name:
        params["name"] = name
    return _recording_call("mark_playhead", params)


@mcp.tool()
@telemetry_tool("list_take_lanes")
@trajectory_tool("list_take_lanes", modifying=False)
def list_take_lanes(
    ctx: Context,
    track_index: Optional[int] = None,
    name: str = "",
    user_prompt: str = "",
) -> str:
    """List arrangement take lanes. Deleting or activating a lane is reported, not assumed."""
    params = {}
    if track_index is not None:
        params["track_index"] = track_index
    if name:
        params["name"] = name
    return _recording_call("list_take_lanes", params)


@mcp.tool()
@telemetry_tool("label_take_lane")
@trajectory_tool("label_take_lane")
def label_take_lane(
    ctx: Context,
    track_index: Optional[int] = None,
    name: str = "",
    index: Optional[int] = None,
    label: str = "KEEP",
    user_prompt: str = "",
) -> str:
    """Rename a take lane. Default is the last lane, label KEEP. Does not move clips."""
    params = {"label": label}
    if track_index is not None:
        params["track_index"] = track_index
    if name:
        params["name"] = name
    if index is not None:
        params["index"] = index
    return _recording_call("label_take_lane", params)


@mcp.tool()
@telemetry_tool("delete_take_lane")
@trajectory_tool("delete_take_lane")
def delete_take_lane(
    ctx: Context,
    track_index: Optional[int] = None,
    name: str = "",
    index: Optional[int] = None,
    user_prompt: str = "",
) -> str:
    """Delete a take lane only if this Live object exposes delete. Otherwise report that it cannot."""
    params = {}
    if track_index is not None:
        params["track_index"] = track_index
    if name:
        params["name"] = name
    if index is not None:
        params["index"] = index
    return _recording_call("delete_take_lane", params)


@mcp.tool()
@telemetry_tool("stop_performance")
@trajectory_tool("stop_performance")
def stop_performance(ctx: Context, user_prompt: str = "") -> str:
    """Stop recording and delete only the take that is being recorded. Playback stays."""
    return _recording_call("stop_performance", {})


@mcp.tool()
@telemetry_tool("keep_last")
@trajectory_tool("keep_last")
def keep_last(ctx: Context, user_prompt: str = "") -> str:
    """Append KEEP to the last finished take. Does not move the clip."""
    return _recording_call("keep_last", {})


@mcp.tool()
@rich_telemetry_tool("get_browser_tree")
@trajectory_tool("get_browser_tree")
def get_browser_tree(ctx: Context, category_type: str = "all", user_prompt: str = "") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_tree", {
            "category_type": category_type
        })
        
        # Check if we got any categories
        if "available_categories" in result and len(result.get("categories", [])) == 0:
            available_cats = result.get("available_categories", [])
            return (f"No categories found for '{category_type}'. "
                   f"Available browser categories: {', '.join(available_cats)}")
        
        # Format the tree in a more readable way
        total_folders = result.get("total_folders", 0)
        formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"
        
        def format_tree(item, indent=0):
            output = ""
            if item:
                prefix = "  " * indent
                name = item.get("name", "Unknown")
                path = item.get("path", "")
                has_more = item.get("has_more", False)
                
                # Add this item
                output += f"{prefix}• {name}"
                if path:
                    output += f" (path: {path})"
                if has_more:
                    output += " [...]"
                output += "\n"
                
                # Add children
                for child in item.get("children", []):
                    output += format_tree(child, indent + 1)
            return output
        
        # Format each category
        for category in result.get("categories", []):
            formatted_output += format_tree(category)
            formatted_output += "\n"
        
        return formatted_output
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        else:
            logger.error(f"Error getting browser tree: {error_msg}")
            return f"Error getting browser tree: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("get_browser_items_at_path")
@trajectory_tool("get_browser_items_at_path")
def get_browser_items_at_path(ctx: Context, path: str, user_prompt: str = "") -> str:
    """
    Get browser items at a specific path in Ableton's browser.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_items_at_path", {
            "path": path
        })
        
        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")
        
        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("load_drum_kit")
@trajectory_tool("load_drum_kit")
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str, user_prompt: str = "") -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        
        # Step 1: Load the drum rack
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })
        
        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"
        
        # Step 2: Get the drum kit items at the specified path
        kit_result = ableton.send_command("get_browser_items_at_path", {
            "path": kit_path
        })
        
        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"
        
        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]
        
        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"
        
        # Step 4: Load the first loadable kit
        kit_uri = loadable_kits[0].get("uri")
        load_result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })
        
        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"

# ── Arrangement view tools ────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("switch_to_arrangement_view")
@trajectory_tool("switch_to_arrangement_view")
def switch_to_arrangement_view(ctx: Context, user_prompt: str = "") -> str:
    """Switch Ableton's main window to the Arrangement view.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("switch_to_arrangement_view")
        return "Switched to Arrangement view"
    except Exception as e:
        logger.error(f"Error switching to arrangement view: {str(e)}")
        return f"Error switching to arrangement view: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_arrangement_time")
@trajectory_tool("set_arrangement_time")
def set_arrangement_time(ctx: Context, time: float, user_prompt: str = "") -> str:
    """
    Move the arrangement playhead to a specific position.

    Parameters:
    - time: Position in beats from the start of the arrangement (e.g. 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_current_song_time", {"time": time})
        return f"Playhead moved to beat {result.get('current_song_time', time)}"
    except Exception as e:
        logger.error(f"Error setting arrangement time: {str(e)}")
        return f"Error setting arrangement time: {str(e)}"


@mcp.tool()
@telemetry_tool("get_arrangement_clips")
@trajectory_tool("get_arrangement_clips")
def get_arrangement_clips(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    List all clips placed in the Arrangement timeline for a track.

    Returns each clip's name, start_time, end_time, length, and type.

    Parameters:
    - track_index: The index of the track to inspect
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_arrangement_clips", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting arrangement clips: {str(e)}")
        return f"Error getting arrangement clips: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("duplicate_to_arrangement")
@trajectory_tool("duplicate_to_arrangement")
def duplicate_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float,
    user_prompt: str = ""
) -> str:
    """
    Copy a Session-view clip into the Arrangement timeline.

    Uses Live's track.duplicate_clip_to_arrangement() API (Live 11 / 12).
    The clip is placed at destination_time beats from the start of the
    arrangement on the same track it lives in.

    Typical workflow:
      1. create_clip / add_notes_to_clip to build a Session clip
      2. Call duplicate_to_arrangement once per bar/section you need
      3. Call switch_to_arrangement_view to confirm the result in Live

    Parameters:
    - track_index:       Index of the track that owns the Session clip
    - clip_index:        Index of the clip slot in that track (Session view)
    - destination_time:  Beat position in the arrangement to place the clip
                         (e.g. 0.0 = start, 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "duplicate_session_clip_to_arrangement",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time
            }
        )
        clip_name = result.get("clip_name", "clip")
        track_name = result.get("track_name", f"track {track_index}")
        return (
            f"Duplicated '{clip_name}' from Session slot {clip_index} "
            f"on '{track_name}' to arrangement at beat {destination_time}"
        )
    except Exception as e:
        logger.error(f"Error duplicating clip to arrangement: {str(e)}")
        return f"Error duplicating clip to arrangement: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("create_locator")
def create_locator(
    ctx: Context,
    name: str,
    time: float,
    user_prompt: str = ""
) -> str:
    """
    Create a named locator (cue point) in the Arrangement at a beat position.

    If a locator already exists at that beat (within ~1e-3 tolerance) it is
    renamed instead of toggled off. Time is in beats from the start of the
    arrangement (e.g. 0.0 = start, 16.0 = bar 5 in 4/4).

    Parameters:
    - name: The locator label (e.g. "Chorus", "Verse 1", "Drop")
    - time: Beat position where the locator should sit
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("create_locator")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "create_locator",
            {"name": name, "time": time}
        )
        return (
            f"Locator '{result.get('name', name)}' set at beat "
            f"{result.get('time', time)}"
        )
    except Exception as e:
        logger.error(f"Error creating locator: {str(e)}")
        return f"Error creating locator: {str(e)}"


# ── Dataset / preference tools (Supabase trajectory recording) ─────────────────

@mcp.tool()
@telemetry_tool("submit_intent")
@trajectory_tool("submit_intent", modifying=False)
def submit_intent(
    ctx: Context,
    text: str,
    level: int = 5,
    user_prompt: str = "",
) -> str:
    """
    Record the human's creative intent for subsequent actions in this session.

    Call this before a sequence of edits so trajectory steps are conditioned on intent.
    Levels: 1=atomic, 2=musical op, 3=section, 4=song, 5=creative goal.

    Requires telemetry consent. Data is stored in Supabase (same project as telemetry).

    Parameters:
    - text: Natural-language intent (e.g. "make the chorus feel bigger")
    - level: Hierarchical intent level 1–5 (default 5)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.set_intent(text=text, level=level)
        return (
            f"Recorded intent {event.intent_id} (level {level}): {text!r}. "
            f"Session: {recorder.session_id}"
        )
    except Exception as e:
        logger.error(f"Error submitting intent: {str(e)}")
        return f"Error submitting intent: {str(e)}"


@mcp.tool()
@telemetry_tool("rate_last_action")
@trajectory_tool("rate_last_action", modifying=False)
def rate_last_action(
    ctx: Context,
    rating: str,
    tags: str = "",
    note: str = "",
    user_prompt: str = "",
) -> str:
    """
    Rate the most recent recorded action (or the track after an edit).

    Ratings: better | same | worse | keep | reject | thumbs_up | thumbs_down
    Optional tags (comma-separated): groove, harmony, melody, sound, arrangement,
    energy, mix, emotion

    Requires telemetry consent.

    Parameters:
    - rating: Preference label
    - tags: Comma-separated aspect tags
    - note: Optional free-text reason
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        event = recorder.record_preference(
            rating=rating.strip().lower(),
            tags=tag_list,
            note=note or None,
        )
        target = event.target_action_id or "none"
        return f"Recorded preference {rating!r} for action {target}"
    except Exception as e:
        logger.error(f"Error rating action: {str(e)}")
        return f"Error rating action: {str(e)}"


@mcp.tool()
@telemetry_tool("prefer_candidate")
@trajectory_tool("prefer_candidate", modifying=False)
def prefer_candidate(
    ctx: Context,
    candidate_a: str,
    candidate_b: str,
    winner: str,
    reason: str = "",
    user_prompt: str = "",
) -> str:
    """
    Record a pairwise preference between two candidate actions or auditions.

    winner should be 'a', 'b', or an explicit candidate id matching A or B.

    Requires telemetry consent.

    Parameters:
    - candidate_a: Id / URI / label for option A
    - candidate_b: Id / URI / label for option B
    - winner: 'a', 'b', or the winning id
    - reason: Optional reason ("C has the right attack")
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        w = winner.strip().lower()
        if w == "a":
            w = candidate_a
        elif w == "b":
            w = candidate_b
        event = recorder.record_preference(
            rating="pairwise",
            winner=w,
            candidate_a=candidate_a,
            candidate_b=candidate_b,
            note=reason or None,
        )
        return f"Recorded pairwise preference: winner={w!r} (event {event.event_id})"
    except Exception as e:
        logger.error(f"Error recording pairwise preference: {str(e)}")
        return f"Error recording pairwise preference: {str(e)}"


@mcp.tool()
@telemetry_tool("reject_last_action")
@trajectory_tool("reject_last_action", modifying=False)
def reject_last_action(
    ctx: Context,
    reason: str = "",
    user_prompt: str = "",
) -> str:
    """
    Mark the last action as rejected (exploration / preference boundary).

    Use when the human undoes or discards an agent edit. Requires telemetry consent.

    Parameters:
    - reason: Optional why it was rejected
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.record_preference(
            rating="reject",
            note=reason or None,
        )
        return f"Recorded rejection for action {event.target_action_id or 'none'}"
    except Exception as e:
        logger.error(f"Error rejecting action: {str(e)}")
        return f"Error rejecting action: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("record_audition")
@trajectory_tool("record_audition", modifying=False)
def record_audition(
    ctx: Context,
    uri: str,
    kept: bool = False,
    search_query: str = "",
    dwell_ms: float = 0.0,
    user_prompt: str = "",
) -> str:
    """
    Log a browser/preset/sample audition (keep or reject) for preference learning.

    Call once per candidate auditioned. Does not load the device — use
    load_instrument_or_effect when kept=True and you want to commit.

    Requires telemetry consent.

    Parameters:
    - uri: Browser item URI (or stable preset/sample id)
    - kept: Whether this candidate was kept
    - search_query: Optional search text that led here (e.g. "analog bass")
    - dwell_ms: Optional time spent auditioning
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.record_audition(
            uri=uri,
            kept=kept,
            search_query=search_query or None,
            dwell_ms=dwell_ms if dwell_ms > 0 else None,
        )
        status = "kept" if kept else "rejected"
        return f"Recorded audition ({status}) uri={uri!r} event={event.event_id}"
    except Exception as e:
        logger.error(f"Error recording audition: {str(e)}")
        return f"Error recording audition: {str(e)}"



# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()