# AbletonMCP/init.py
from __future__ import absolute_import, print_function, unicode_literals

from _Framework.ControlSurface import ControlSurface
import os
import socket
import json
import threading
import time
import traceback

# Change queue import for Python 2
try:
    import Queue as queue  # Python 2
except ImportError:
    import queue  # Python 3

# Constants for socket communication
DEFAULT_PORT = 9877
# Loopback, not 0.0.0.0. This socket takes unauthenticated commands that import
# arbitrary absolute paths, load browser items and drive the transport, so
# anything that can reach the port controls the DAW. Binding every interface
# exposed that to the whole local network — and to any network the machine is
# routable from.
#
# Running Live and the MCP server on different machines is the one legitimate
# reason to widen this; set ABLETON_MCP_HOST to do so deliberately, and put it
# behind a trusted network or an explicit relay.
HOST = os.environ.get("ABLETON_MCP_HOST", "127.0.0.1")

# A command that never completes must not grow the buffer without bound.
# Generous enough for a large add_notes_to_clip payload.
MAX_REQUEST_BYTES = 16 * 1024 * 1024

# Bumped whenever the TCP command surface changes; the MCP server compares
# this to EXPECTED_REMOTE_SCRIPT_VERSION.
SCRIPT_VERSION = "2.0.0"
PROTOCOL_VERSION = 1

# Live 12 Python API (not the older Max LOM wording):
#   Song.record_mode            arrangement record button, get/set
#   Song.session_record         session record state, get/set
#   Song.session_record_status  off=0, on=1, transition=2, get only
#   Song.arrangement_overdub    arrangement overdub, get/set
#   Song.overdub                legacy alias of session record; not set here
#   Song.count_in_duration      get/observe only (0 none, 1 one bar, 2 two, 3 four)
#   Song.trigger_session_record is a toggle (a second call stops and plays).
#     Empty-slot recording uses ClipSlot.fire instead, which takes an explicit slot.
#   ClipSlot.fire(record_length) records that empty slot for record_length beats
#     and raises if the slot already owns a clip.
#   Track.current_monitoring_state  0=in, 1=auto, 2=off
# Clip.crop and audio-file replacement are intentionally not exposed.

# Open-ended fire() is ClipSlot.fire with no args. A passed length is beats.
MAX_EMPTY_SLOT_RECORD_BEATS = 16384
# Live 12 briefly reports clip.length around 63072000 (sample frames, not
# beats) while a take is still opening. Lengths above this during recording
# are reported as unsettled instead of as a beat length.
_UNSETTLED_RECORD_LENGTH = 100000.0
# stop_playing() can leave is_playing true for a moment. Re-read a few times
# on the main thread, then mark the value pending if it has not settled.
# Song.clip_trigger_quantization 0 = None. Passed only to this fire() call so
# a fixed record_length is not shortened by the global launch grid. The song
# setting itself is not written.
_LAUNCH_QUANTIZATION_NONE = 0
# Safety ceiling when record_loop is started without max_takes.
_RECORD_LOOP_HARD_CAP = 128
# Voice takes. Count-in uses Song.count_in_duration when the write sticks;
# otherwise the 1.9.0 pre-roll path runs and the reply says which one.
DEFAULT_TAKE_BARS = 8
DEFAULT_COUNT_IN_BARS = 1

_MONITORING_STATE_BY_NAME = {"in": 0, "auto": 1, "off": 2}
_MONITORING_NAME_BY_STATE = {0: "in", 1: "auto", 2: "off"}
_COUNT_IN_LABELS = {0: "none", 1: "1_bar", 2: "2_bars", 3: "4_bars"}
_SESSION_RECORD_STATUS_NAMES = {0: "off", 1: "on", 2: "transition"}
_STOP_RECORDING_SCOPES = ("arrangement", "session", "slot", "recording")
_CLIP_REGION_VIEWS = ("session", "arrangement")

# New commands. Every one runs on Live's main thread via schedule_message.
RECORDING_COMMANDS = (
    "get_recording_state",
    "get_track_mixer_state",
    "set_track_arm",
    "set_track_mute",
    "set_track_solo",
    "set_track_volume",
    "set_track_panning",
    "set_track_send",
    "set_track_monitoring",
    "set_track_input_routing",
    "set_arrangement_record",
    "set_session_record",
    "set_arrangement_overdub",
    "record_empty_slot",
    "stop_recording",
    "prepare_track",
    "get_clip_region",
    "set_clip_region",
    "quick_record",
    "quick_arrangement_record",
    "quick_arrangement_stop",
    "set_mixer_batch",
    "record_loop",
    "stop_record_loop",
    "stop_take",
    "set_metronome",
    "set_count_in",
    "create_scene",
    "keep_take",
    "list_takes",
    "get_performance_state",
    "show_view",
    "arm_tracks",
    "take",
    "again",
    "scrap",
    "cancel",
    "play_transport",
    "pause_transport",
    "nudge_tempo",
    "mark_playhead",
    "list_take_lanes",
    "label_take_lane",
    "delete_take_lane",
    "stop_performance",
    "keep_last",
)

SCRIPT_CAPABILITIES = [
    "get_session_info",
    "get_track_info",
    "get_script_info",
    "get_clip_notes",
    "get_device_parameters",
    "get_session_snapshot",
    "set_device_parameter",
    "drain_passive_events",
    "create_midi_track",
    "create_audio_track",
    "create_clip",
    "create_audio_clip",
    "add_notes_to_clip",
    "load_instrument_or_effect",
    "get_arrangement_clips",
    "duplicate_session_clip_to_arrangement",
    "create_locator",
    "delete_clip",
    "clear_notes_from_clip",
] + list(RECORDING_COMMANDS)

def create_instance(c_instance):
    """Create and return the AbletonMCP script instance"""
    return AbletonMCP(c_instance)

class AbletonMCP(ControlSurface):
    """AbletonMCP Remote Script for Ableton Live"""
    
    def __init__(self, c_instance):
        """Initialize the control surface"""
        ControlSurface.__init__(self, c_instance)
        self.log_message(
            "AbletonMCP Remote Script initializing... (script v%s)"
            % SCRIPT_VERSION
        )
        
        # Socket server for communication
        self.server = None
        self.client_threads = []
        self.server_thread = None
        self.running = False
        
        # Cache the song reference for easier access
        self._song = self.song()

        # Passive human-UI event queue (drained by MCP → Supabase)
        self._passive_events = []
        self._passive_lock = threading.Lock()
        self._passive_max = 500
        self._passive_track_count = None
        self._passive_track_bindings = []  # (track, [(add_name, callback), ...]) for cleanup
        self._song_passive_callbacks = []
        
        # Start the socket server
        self.start_server()

        # Register LOM listeners for passive capture (after song is ready)
        try:
            self._setup_passive_listeners()
        except Exception as e:
            self.log_message("Passive listener setup failed: " + str(e))
            self.log_message(traceback.format_exc())
        
        self.log_message("AbletonMCP initialized")
        
        # Show a message in Ableton
        self.show_message("AbletonMCP: Listening for commands on port " + str(DEFAULT_PORT))
    
    def disconnect(self):
        """Called when Ableton closes or the control surface is removed"""
        self.log_message("AbletonMCP disconnecting...")
        self.running = False

        try:
            self._teardown_passive_listeners()
        except Exception as e:
            self.log_message("Passive listener teardown error: " + str(e))
        
        # Stop the server
        if self.server:
            try:
                self.server.close()
            except:
                pass
        
        # Wait for the server thread to exit
        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(1.0)
            
        # Clean up any client threads
        for client_thread in self.client_threads[:]:
            if client_thread.is_alive():
                # We don't join them as they might be stuck
                self.log_message("Client thread still alive during disconnect")
        
        ControlSurface.disconnect(self)
        self.log_message("AbletonMCP disconnected")
    
    def start_server(self):
        """Start the socket server in a separate thread"""
        try:
            self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server.bind((HOST, DEFAULT_PORT))
            self.server.listen(5)  # Allow up to 5 pending connections
            
            self.running = True
            self.server_thread = threading.Thread(target=self._server_thread)
            self.server_thread.daemon = True
            self.server_thread.start()
            
            self.log_message("Server started on port " + str(DEFAULT_PORT))
        except Exception as e:
            self.log_message("Error starting server: " + str(e))
            self.show_message("AbletonMCP: Error starting server - " + str(e))
    
    def _server_thread(self):
        """Server thread implementation - handles client connections"""
        try:
            self.log_message("Server thread started")
            # Set a timeout to allow regular checking of running flag
            self.server.settimeout(1.0)
            
            while self.running:
                try:
                    # Accept connections with timeout
                    client, address = self.server.accept()
                    self.log_message("Connection accepted from " + str(address))
                    self.show_message("AbletonMCP: Client connected")
                    
                    # Handle client in a separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                    # Keep track of client threads
                    self.client_threads.append(client_thread)
                    
                    # Clean up finished client threads
                    self.client_threads = [t for t in self.client_threads if t.is_alive()]
                    
                except socket.timeout:
                    # No connection yet, just continue
                    continue
                except Exception as e:
                    if self.running:  # Only log if still running
                        self.log_message("Server accept error: " + str(e))
                    time.sleep(0.5)
            
            self.log_message("Server thread stopped")
        except Exception as e:
            self.log_message("Server thread error: " + str(e))
    
    def _handle_client(self, client):
        """Handle communication with a connected client"""
        self.log_message("Client handler started")
        client.settimeout(None)  # No timeout for client socket
        # Accumulate raw bytes, never per-chunk text. A multi-byte UTF-8
        # character (an accented clip name, an emoji) can straddle two recv()
        # boundaries; decoding each chunk on its own raises there, and the old
        # code answered that with an error frame pushed into a stream the
        # client was still reading a real response from — desynchronising the
        # connection for good.
        buffer = b''

        try:
            while self.running:
                try:
                    # Receive data
                    data = client.recv(8192)

                    if not data:
                        # Client disconnected
                        self.log_message("Client disconnected")
                        break

                    buffer += data

                    if len(buffer) > MAX_REQUEST_BYTES:
                        buffer = b''
                        raise ValueError(
                            "Request exceeded %d bytes without a complete JSON command"
                            % MAX_REQUEST_BYTES)

                    try:
                        # Only a fully received command decodes and parses.
                        # Either error means "not all here yet", so wait.
                        command = json.loads(buffer.decode('utf-8'))
                    except (ValueError, UnicodeDecodeError):
                        continue

                    buffer = b''  # Clear buffer after successful parse

                    self.log_message("Received command: " + str(command.get("type", "unknown")))

                    # Process the command and get response
                    response = self._process_command(command)

                    # Send the response with explicit encoding
                    client.sendall(json.dumps(response).encode('utf-8'))

                except Exception as e:
                    self.log_message("Error handling client data: " + str(e))
                    self.log_message(traceback.format_exc())

                    # Every error that reaches here leaves the request/response
                    # stream in an unknown state, so report it and close rather
                    # than carry on out of step with the client.
                    error_response = {
                        "status": "error",
                        "message": str(e)
                    }
                    try:
                        client.sendall(json.dumps(error_response).encode('utf-8'))
                    except Exception:
                        pass
                    break
        except Exception as e:
            self.log_message("Error in client handler: " + str(e))
        finally:
            try:
                client.close()
            except:
                pass
            self.log_message("Client handler stopped")
    
    def _process_command(self, command):
        """Process a command from the client and return a response"""
        command_type = command.get("type", "")
        params = command.get("params", {})
        
        # Initialize response
        response = {
            "status": "success",
            "result": {}
        }
        
        try:
            # Recording / mixer / routing / clip-region commands always run on
            # Live's main thread. Invalid params fail before any Live write.
            if command_type == "stop_playback":
                return self._confirmed_stop_playback()
            if command_type in RECORDING_COMMANDS:
                return self._dispatch_recording_command(command_type, params)
            # Route the command to the appropriate handler
            if command_type == "get_script_info":
                response["result"] = self._get_script_info()
            elif command_type == "get_session_info":
                response["result"] = self._get_session_info()
            elif command_type == "get_track_info":
                track_index = params.get("track_index", 0)
                response["result"] = self._get_track_info(track_index)
            # Commands that modify Live's state should be scheduled on the main thread
            elif command_type in ["create_midi_track", "create_audio_track", "set_track_name",
                                 "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
                                 "set_arrangement_clip_name",
                                 "delete_clip",
                                 "clear_notes_from_clip",
                                 "set_tempo", "fire_clip", "stop_clip",
                                 "start_playback", "stop_playback",
                                 "load_browser_item", "load_instrument_or_effect",
                                 # Arrangement view – must run on the main thread
                                 "switch_to_arrangement_view", "set_current_song_time",
                                 "duplicate_session_clip_to_arrangement",
                                 "map_rack_magnitude", "inspect_rack",
                                 "create_locator"]:
                # Use a thread-safe approach with a response queue
                response_queue = queue.Queue()
                
                # Define a function to execute on the main thread
                def main_thread_task():
                    try:
                        result = None
                        if command_type == "create_midi_track":
                            index = params.get("index", -1)
                            result = self._create_midi_track(index)
                        elif command_type == "create_audio_track":
                            index = params.get("index", -1)
                            result = self._create_audio_track(index)
                        elif command_type == "set_track_name":
                            track_index = params.get("track_index", 0)
                            name = params.get("name", "")
                            result = self._set_track_name(track_index, name)
                        elif command_type == "create_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            length = params.get("length", 4.0)
                            result = self._create_clip(track_index, clip_index, length)
                        elif command_type == "create_audio_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            path = params.get("path", "")
                            result = self._create_audio_clip(track_index, clip_index, path)
                        elif command_type == "add_notes_to_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            notes = params.get("notes", [])
                            result = self._add_notes_to_clip(track_index, clip_index, notes)
                        elif command_type == "clear_notes_from_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._clear_notes_from_clip(track_index, clip_index)
                        elif command_type == "set_clip_name":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            name = params.get("name", "")
                            result = self._set_clip_name(track_index, clip_index, name)
                        elif command_type == "set_arrangement_clip_name":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            name = params.get("name", "")
                            result = self._set_arrangement_clip_name(track_index, clip_index, name)
                        elif command_type == "set_tempo":
                            tempo = params.get("tempo", 120.0)
                            result = self._set_tempo(tempo)
                        elif command_type == "fire_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._fire_clip(track_index, clip_index)
                        elif command_type == "stop_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._stop_clip(track_index, clip_index)
                        elif command_type == "delete_clip":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            result = self._delete_clip(track_index, clip_index)
                        elif command_type == "start_playback":
                            result = self._start_playback()
                        elif command_type == "stop_playback":
                            result = self._stop_playback()
                        elif command_type == "load_instrument_or_effect":
                            track_index = params.get("track_index", 0)
                            uri = params.get("uri", "")
                            result = self._load_instrument_or_effect(track_index, uri)
                        elif command_type == "load_browser_item":
                            track_index = params.get("track_index", 0)
                            item_uri = params.get("item_uri", "")
                            result = self._load_browser_item(track_index, item_uri)
                        # ── Arrangement view commands ──────────────────────────────
                        elif command_type == "switch_to_arrangement_view":
                            result = self._switch_to_arrangement_view()
                        elif command_type == "set_current_song_time":
                            time_val = params.get("time", 0.0)
                            result = self._set_current_song_time(time_val)
                        elif command_type == "duplicate_session_clip_to_arrangement":
                            track_index = params.get("track_index", 0)
                            clip_index = params.get("clip_index", 0)
                            destination_time = params.get("destination_time", 0.0)
                            result = self._duplicate_session_clip_to_arrangement(
                                track_index, clip_index, destination_time)
                        elif command_type == "map_rack_magnitude":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            macro_name = params.get("macro_name", "Magnitude")
                            result = self._map_rack_magnitude(
                                track_index, device_index, macro_name)
                        elif command_type == "inspect_rack":
                            track_index = params.get("track_index", 0)
                            device_index = params.get("device_index", 0)
                            result = self._inspect_rack(track_index, device_index)
                        elif command_type == "create_locator":
                            name = params.get("name", "")
                            time_val = params.get("time", 0.0)
                            result = self._create_locator(name, time_val)

                        # Put the result in the queue
                        response_queue.put({"status": "success", "result": result})
                    except Exception as e:
                        self.log_message("Error in main thread task: " + str(e))
                        self.log_message(traceback.format_exc())
                        response_queue.put({"status": "error", "message": str(e)})
                
                # Schedule the task to run on the main thread
                try:
                    self.schedule_message(0, main_thread_task)
                except AssertionError:
                    # If we're already on the main thread, execute directly
                    main_thread_task()
                
                # create_audio_clip decodes/imports the file on the main
                # thread and needs more than the default headroom.
                long_running_commands = {"create_audio_clip": 60.0}
                queue_timeout = long_running_commands.get(command_type, 10.0)
                try:
                    task_response = response_queue.get(timeout=queue_timeout)
                    if task_response.get("status") == "error":
                        response["status"] = "error"
                        response["message"] = task_response.get("message", "Unknown error")
                    else:
                        response["result"] = task_response.get("result", {})
                except queue.Empty:
                    response["status"] = "error"
                    response["message"] = "Timeout waiting for operation to complete"
            elif command_type == "get_browser_item":
                uri = params.get("uri", None)
                path = params.get("path", None)
                response["result"] = self._get_browser_item(uri, path)
            elif command_type == "get_browser_categories":
                category_type = params.get("category_type", "all")
                response["result"] = self._get_browser_categories(category_type)
            elif command_type == "get_browser_items":
                path = params.get("path", "")
                item_type = params.get("item_type", "all")
                response["result"] = self._get_browser_items(path, item_type)
            # Add the new browser commands
            elif command_type == "get_browser_tree":
                category_type = params.get("category_type", "all")
                response["result"] = self.get_browser_tree(category_type)
            elif command_type == "get_browser_items_at_path":
                path = params.get("path", "")
                response["result"] = self.get_browser_items_at_path(path)
            # Read-only arrangement command – no main-thread scheduling required
            elif command_type == "get_arrangement_clips":
                track_index = params.get("track_index", 0)
                response["result"] = self._get_arrangement_clips(track_index)
            # Dataset / state-snapshot reads
            elif command_type == "get_clip_notes":
                track_index = params.get("track_index", 0)
                clip_index = params.get("clip_index", 0)
                response["result"] = self._get_clip_notes(track_index, clip_index)
            elif command_type == "get_device_parameters":
                track_index = params.get("track_index", 0)
                device_index = params.get("device_index", 0)
                response["result"] = self._get_device_parameters(track_index, device_index)
            elif command_type == "get_session_snapshot":
                include_notes = params.get("include_notes", True)
                include_params = params.get("include_params", True)
                response["result"] = self._get_session_snapshot(
                    include_notes=include_notes,
                    include_params=include_params,
                )
            elif command_type == "drain_passive_events":
                response["result"] = self._drain_passive_events()
            elif command_type == "set_device_parameter":
                response_queue = queue.Queue()

                def main_thread_task():
                    try:
                        result = self._set_device_parameter(
                            params.get("track_index", 0),
                            params.get("device_index", 0),
                            params.get("parameter_index", 0),
                            params.get("value", 0.0),
                        )
                        response_queue.put({"status": "success", "result": result})
                    except Exception as e:
                        self.log_message("Error in main thread task: " + str(e))
                        self.log_message(traceback.format_exc())
                        response_queue.put({"status": "error", "message": str(e)})

                try:
                    self.schedule_message(0, main_thread_task)
                except AssertionError:
                    main_thread_task()

                try:
                    task_response = response_queue.get(timeout=10.0)
                    if task_response.get("status") == "error":
                        response["status"] = "error"
                        response["message"] = task_response.get("message", "Unknown error")
                    else:
                        response["result"] = task_response.get("result", {})
                except queue.Empty:
                    response["status"] = "error"
                    response["message"] = "Timeout waiting for operation to complete"
            else:
                response["status"] = "error"
                response["message"] = "Unknown command: " + command_type
        except Exception as e:
            self.log_message("Error processing command: " + str(e))
            self.log_message(traceback.format_exc())
            response["status"] = "error"
            response["message"] = str(e)
        
        return response
    
    # Command implementations

    def _get_script_info(self):
        """Handshake payload for MCP server version / capability checks."""
        return {
            "name": "AbletonMCP",
            "script_version": SCRIPT_VERSION,
            "protocol_version": PROTOCOL_VERSION,
            "port": DEFAULT_PORT,
            "capabilities": list(SCRIPT_CAPABILITIES),
            "snapshot_schema": "ableton_mcp_snapshot_v2",
            "passive_listeners": True,
            "recording": {
                "commands": list(RECORDING_COMMANDS),
                "count_in_writable": False,
                "count_in_duration_labels": dict(
                    (str(k), v) for k, v in _COUNT_IN_LABELS.items()
                ),
                "monitoring_states": dict(_MONITORING_STATE_BY_NAME),
                "fixed_length_empty_slot": True,
                "uses_trigger_session_record": False,
                "crop": False,
                "replaces_source_files": False,
                "realtime": False,
            },
        }
    
    def _safe_song_property(self, attr, cast, default):
        """Read self._song.<attr> with cast, returning default on common failures.
        Catches only narrow exceptions so genuine bugs still surface."""
        try:
            return cast(getattr(self._song, attr))
        except (AttributeError, TypeError, ValueError):
            return default

    def _get_session_info(self):
        """Get information about the current session"""
        try:
            result = {
                "tempo": self._song.tempo,
                "signature_numerator": self._song.signature_numerator,
                "signature_denominator": self._song.signature_denominator,
                "track_count": len(self._song.tracks),
                "return_track_count": len(self._song.return_tracks),
                "master_track": {
                    "name": "Master",
                    "volume": self._song.master_track.mixer_device.volume.value,
                    "panning": self._song.master_track.mixer_device.panning.value
                },
                # Read via _safe_song_property so an attribute missing on a
                # given Live version falls back to its default.
                "is_playing":        self._safe_song_property("is_playing",        bool,  False),
                "current_song_time": self._safe_song_property("current_song_time", float, 0.0),
                "song_length":       self._safe_song_property("song_length",       float, 0.0),
                "loop":              self._safe_song_property("loop",              bool,  False),
                "loop_start":        self._safe_song_property("loop_start",        float, 0.0),
                "loop_length":       self._safe_song_property("loop_length",       float, 0.0),
                # Additive recording flags. A missing or unreadable attribute
                # stays at the default so this existing command still returns.
                # Richer state, read on the main thread, is get_recording_state.
                "record_mode": self._optional_song_value("record_mode", bool, False),
                "session_record": self._optional_song_value("session_record", bool, False),
                "arrangement_overdub": self._optional_song_value(
                    "arrangement_overdub", bool, False
                ),
                "session_record_status": self._optional_song_value(
                    "session_record_status", int, None
                ),
                "is_counting_in": self._optional_song_value("is_counting_in", bool, False),
                "count_in_duration": self._optional_song_value(
                    "count_in_duration", int, None
                ),
            }
            return result
        except Exception as e:
            self.log_message("Error getting session info: " + str(e))
            raise
    
    def _get_track_info(self, track_index):
        """Get information about a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Get clip slots
            clip_slots = []
            for slot_index, slot in enumerate(track.clip_slots):
                clip_info = None
                if slot.has_clip:
                    clip = slot.clip
                    length_info = self._length_report(clip)
                    clip_info = {
                        "name": clip.name,
                        "length": length_info["length"],
                        "length_status": length_info["length_status"],
                        "is_playing": clip.is_playing,
                        "is_recording": clip.is_recording,
                        # Playback region. Setting these is set_clip_region;
                        # this read does not crop or touch the source file.
                        "start_marker": self._safe_attr(clip, "start_marker", float, None),
                        "end_marker": self._safe_attr(clip, "end_marker", float, None),
                    }
                
                clip_slots.append({
                    "index": slot_index,
                    "has_clip": slot.has_clip,
                    "clip": clip_info
                })
            
            # Get devices
            devices = []
            for device_index, device in enumerate(track.devices):
                devices.append({
                    "index": device_index,
                    "name": device.name,
                    "class_name": device.class_name,
                    "type": self._get_device_type(device)
                })
            
            result = {
                "index": track_index,
                "name": track.name,
                "is_audio_track": track.has_audio_input,
                "is_midi_track": track.has_midi_input,
                "mute": track.mute,
                "solo": track.solo,
                "arm": self._safe_arm(track),
                "volume": track.mixer_device.volume.value,
                "panning": track.mixer_device.panning.value,
                "clip_slots": clip_slots,
                "devices": devices
            }
            return result
        except Exception as e:
            self.log_message("Error getting track info: " + str(e))
            raise
    
    def _safe_arm(self, track):
        """Read track.arm, returning False for tracks that have no arm state.

        Live raises RuntimeError("Master and Return Tracks have no 'Arm'
        state!") for group tracks as well as return and main tracks. A
        `getattr(track, "arm", False)` does not guard this: the attribute
        exists, so getattr's default never applies -- reading it is what
        throws, and the error is a RuntimeError rather than an AttributeError.

        Check can_be_armed first so the common path does not rely on raising,
        and keep a narrow catch for Live versions that do not expose that
        property on every track type.
        """
        try:
            if not getattr(track, "can_be_armed", False):
                return False
            return bool(track.arm)
        except (AttributeError, RuntimeError):
            return False

    def _create_midi_track(self, index):
        """Create a new MIDI track at the specified index"""
        try:
            # Create the track
            self._song.create_midi_track(index)
            
            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]
            
            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating MIDI track: " + str(e))
            raise

    def _create_audio_track(self, index):
        """Create a new audio track at the specified index"""
        try:
            # Create the track
            self._song.create_audio_track(index)

            # Get the new track
            new_track_index = len(self._song.tracks) - 1 if index == -1 else index
            new_track = self._song.tracks[new_track_index]

            result = {
                "index": new_track_index,
                "name": new_track.name
            }
            return result
        except Exception as e:
            self.log_message("Error creating audio track: " + str(e))
            raise


    def _set_track_name(self, track_index, name):
        """Set the name of a track"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            # Set the name
            track = self._song.tracks[track_index]
            track.name = name
            
            result = {
                "name": track.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting track name: " + str(e))
            raise
    
    def _create_clip(self, track_index, clip_index, length):
        """Create a new MIDI clip in the specified track and clip slot"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            # Check if the clip slot already has a clip
            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")
            
            # Create the clip
            clip_slot.create_clip(length)
            
            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length
            }
            return result
        except Exception as e:
            self.log_message("Error creating clip: " + str(e))
            raise

    def _create_audio_clip(self, track_index, clip_index, path):
        """Create an audio clip in the specified audio track clip slot by importing a file.

        Requires Ableton Live 12.0.5 or newer (the underlying
        ClipSlot.create_audio_clip Live API was introduced in 12.0.5 — it is
        not available in earlier 12.0.x releases).
        """
        try:
            if not path:
                raise ValueError("Audio file path is required")

            if not os.path.isabs(path):
                raise ValueError("Audio file path must be absolute (got: %s)" % path)

            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if getattr(track, "has_midi_input", False) or not getattr(track, "has_audio_input", True):
                raise ValueError("Track %d is not an audio track" % track_index)

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if clip_slot.has_clip:
                raise Exception("Clip slot already has a clip")

            if not hasattr(clip_slot, "create_audio_clip"):
                raise Exception(
                    "ClipSlot.create_audio_clip is unavailable in this Ableton Live "
                    "version. Requires Live 12.0.5 or newer."
                )

            clip_slot.create_audio_clip(path)

            result = {
                "name": clip_slot.clip.name,
                "length": clip_slot.clip.length,
                "is_audio_clip": clip_slot.clip.is_audio_clip
            }
            return result
        except Exception as e:
            self.log_message("Error creating audio clip: " + str(e))
            raise

    def _add_notes_to_clip(self, track_index, clip_index, notes):
        """Add MIDI notes to a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            
            # Convert note data to Live's format
            live_notes = []
            for note in notes:
                pitch = note.get("pitch", 60)
                start_time = note.get("start_time", 0.0)
                duration = note.get("duration", 0.25)
                velocity = note.get("velocity", 100)
                mute = note.get("mute", False)
                
                live_notes.append((pitch, start_time, duration, velocity, mute))
            
            # Add the notes
            clip.set_notes(tuple(live_notes))
            
            result = {
                "note_count": len(notes)
            }
            return result
        except Exception as e:
            self.log_message("Error adding notes to clip: " + str(e))
            raise
    
    def _set_clip_name(self, track_index, clip_index, name):
        """Set the name of a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip = clip_slot.clip
            clip.name = name
            
            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting clip name: " + str(e))
            raise

    def _set_arrangement_clip_name(self, track_index, clip_index, name):
        """Set the name of a clip placed in the Arrangement timeline.

        clip_index indexes into track.arrangement_clips, in the same order
        as returned by _get_arrangement_clips (i.e. ordered by start_time).
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            arrangement_clips = list(track.arrangement_clips)

            if clip_index < 0 or clip_index >= len(arrangement_clips):
                raise IndexError("Clip index out of range")

            clip = arrangement_clips[clip_index]
            clip.name = name

            result = {
                "name": clip.name
            }
            return result
        except Exception as e:
            self.log_message("Error setting arrangement clip name: " + str(e))
            raise

    def _set_tempo(self, tempo):
        """Set the tempo of the session"""
        try:
            self._song.tempo = tempo
            
            result = {
                "tempo": self._song.tempo
            }
            return result
        except Exception as e:
            self.log_message("Error setting tempo: " + str(e))
            raise
    
    def _fire_clip(self, track_index, clip_index):
        """Fire a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            if not clip_slot.has_clip:
                raise Exception("No clip in slot")
            
            clip_slot.fire()
            
            result = {
                "fired": True
            }
            return result
        except Exception as e:
            self.log_message("Error firing clip: " + str(e))
            raise
    
    def _stop_clip(self, track_index, clip_index):
        """Stop a clip"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            
            clip_slot = track.clip_slots[clip_index]
            
            clip_slot.stop()
            
            result = {
                "stopped": True
            }
            return result
        except Exception as e:
            self.log_message("Error stopping clip: " + str(e))
            raise

    def _delete_clip(self, track_index, clip_index):
        """Delete the clip in the given clip slot, freeing the slot for reuse."""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                return {"deleted": False, "reason": "Clip slot was already empty"}

            clip_slot.delete_clip()

            return {"deleted": True}
        except Exception as e:
            self.log_message("Error deleting clip: " + str(e))
            raise


    def _start_playback(self):
        """Start playing the session"""
        try:
            self._song.start_playing()
            
            result = {
                "playing": self._song.is_playing
            }
            return result
        except Exception as e:
            self.log_message("Error starting playback: " + str(e))
            raise
    
    def _stop_playback(self):
        """Stop playback. Prefer _confirmed_stop_playback, which re-reads next tick."""
        self._song.stop_playing()
        return self._transport_reply(requested_playing=False, observed_playing=None)

    def _await_main(self, delay, fn, timeout=2.0):
        """Run fn on Live's main thread. The socket thread waits; the main thread does not.

        delay 0 runs on the next message. delay > 0 is a later tick, so a
        transport change from the previous tick is visible. Unit tests queue
        delay > 0 on _deferred_for_tests and this wait pumps that queue.
        """
        box = queue.Queue()

        def task():
            try:
                box.put(("ok", fn()))
            except Exception as exc:
                box.put(("err", exc))

        try:
            self.schedule_message(delay, task)
        except AssertionError:
            task()
        deadline = time.time() + timeout
        while True:
            try:
                status, payload = box.get_nowait()
            except queue.Empty:
                deferred = getattr(self, "_deferred_for_tests", None)
                if deferred:
                    deferred.pop(0)()
                    continue
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise Exception("Timeout waiting for Live main thread")
                try:
                    status, payload = box.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    continue
            if status == "err":
                raise payload
            return payload

    def _schedule_later(self, delay, callback):
        """Queue callback on a later main-thread tick. Never runs it inline here."""
        try:
            self.schedule_message(delay, callback)
        except AssertionError:
            if not hasattr(self, "_deferred_for_tests"):
                self._deferred_for_tests = []
            self._deferred_for_tests.append(callback)

    def _confirmed_arrangement_stop(self, params):
        """Mutate on this tick, read transport on the next tick."""
        box = {}

        def mutate():
            box["result"] = self._cmd_quick_arrangement_stop(params)

        self._await_main(0, mutate)

        def read():
            return (
                self._optional_song_value("is_playing", bool, None),
                self._optional_song_value("record_mode", bool, None),
            )

        try:
            observed_playing, observed_record = self._await_main(1, read)
        except Exception as e:
            self.log_message("arrangement stop confirm failed: " + str(e))
            observed_playing, observed_record = None, None
        result = box["result"]
        result.update(self._transport_reply(
            False, observed_playing, False, observed_record
        ))
        return {"status": "success", "result": result}

    def _confirmed_stop_playback(self):
        """stop_playing on this tick, read is_playing on the next tick."""
        try:
            self._await_main(0, lambda: self._song.stop_playing())
        except Exception as e:
            return {"status": "error", "message": str(e)}
        try:
            observed = self._await_main(1, lambda: bool(self._song.is_playing))
        except Exception as e:
            observed = None
            self.log_message("stop confirm read failed: " + str(e))
        return {
            "status": "success",
            "result": self._transport_reply(False, observed),
        }

    def _transport_reply(self, requested_playing, observed_playing, record_mode=None, observed_record_mode=None):
        """requested_* is what we asked Live to do. observed_* is the next tick.

        The same-tick read is not returned as playing/record_mode. Live 12
        still reports the old transport value until a later message.
        """
        result = {
            "playing": requested_playing,
            "requested_playing": requested_playing,
            "observed_playing": observed_playing,
            "confirmed": observed_playing is requested_playing,
        }
        if record_mode is not None or observed_record_mode is not None:
            result["record_mode"] = record_mode
            result["requested_record_mode"] = record_mode
            result["observed_record_mode"] = observed_record_mode
            result["confirmed"] = (
                result["confirmed"] and observed_record_mode is record_mode
            )
        result["note"] = (
            "playing and record_mode are the requested state. "
            "confirmed is true only when a later main-thread tick already "
            "shows that state. A same-tick read is not used."
        )
        return result
    
    # ── Arrangement view implementations ──────────────────────────────────────

    def _switch_to_arrangement_view(self):
        """Switch Ableton's main window to the Arrangement view"""
        try:
            self.application().view.show_view("Arranger")
            return {"view": "Arranger"}
        except Exception as e:
            self.log_message("Error switching to arrangement view: " + str(e))
            raise

    def _set_current_song_time(self, time_val):
        """Move the arrangement playhead to a position in beats"""
        try:
            self._song.current_song_time = float(time_val)
            return {"current_song_time": self._song.current_song_time}
        except Exception as e:
            self.log_message("Error setting current song time: " + str(e))
            raise

    def _get_arrangement_clips(self, track_index):
        """Return all clips placed in the Arrangement timeline for a track.

        Each clip dict contains:
          name, start_time, end_time, length, color,
          is_midi_clip, is_audio_clip, is_playing
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]
            clips = []

            # track.arrangement_clips is available in Live 11 / 12
            for clip in track.arrangement_clips:
                clips.append({
                    "name": clip.name,
                    "start_time": clip.start_time,
                    "end_time": clip.end_time,
                    "length": clip.length,
                    "color": clip.color,
                    "is_midi_clip": clip.is_midi_clip,
                    "is_audio_clip": clip.is_audio_clip,
                    "is_playing": clip.is_playing,
                    "start_marker": self._safe_attr(clip, "start_marker", float, None),
                    "end_marker": self._safe_attr(clip, "end_marker", float, None),
                    "loop_start": self._safe_attr(clip, "loop_start", float, None),
                    "loop_end": self._safe_attr(clip, "loop_end", float, None),
                    "looping": self._safe_attr(clip, "looping", bool, None),
                })

            return {
                "track_index": track_index,
                "track_name": track.name,
                "clip_count": len(clips),
                "clips": clips
            }
        except Exception as e:
            self.log_message("Error getting arrangement clips: " + str(e))
            raise

    def _clear_notes_from_clip(self, track_index, clip_index):
        """Remove all MIDI notes from a Session clip.

        Pairs with _add_notes_to_clip to make a real replace (clear, then add),
        which the write-only API otherwise can't do. Counts notes first so the
        result can report how many were removed.
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception("No clip in slot")

            clip = clip_slot.clip

            if not clip.is_midi_clip:
                raise Exception("Clip is not a MIDI clip; no notes to clear")

            length = clip.length

            # Count existing notes for the report (best-effort; never fatal).
            cleared = 0
            try:
                getter = getattr(clip, "get_notes_extended", None)
                if getter is not None:
                    cleared = len(list(getter(0, 128, 0.0, length)))
                else:
                    cleared = len(list(clip.get_notes(0.0, 0, length, 128)))
            except Exception:
                cleared = 0

            # Remove every note across the full pitch/time range. Prefer the
            # modern API (Live 11+); fall back to the legacy signature. Argument
            # order mirrors the get/remove _extended family:
            #   remove_notes_extended(from_pitch, pitch_span, from_time, time_span)
            # vs the legacy remove_notes(from_time, from_pitch, time_span, pitch_span).
            remover = getattr(clip, "remove_notes_extended", None)
            if remover is not None:
                remover(0, 128, 0.0, length)
            else:
                clip.remove_notes(0.0, 0, length, 128)

            return {
                "track_index": track_index,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "cleared_count": cleared,
            }
        except Exception as e:
            self.log_message("Error clearing notes from clip: " + str(e))
            raise

    def _duplicate_session_clip_to_arrangement(self, track_index, clip_index, destination_time):
        """Copy a Session-view clip into the Arrangement timeline.

        Uses the real Live API:
          track.duplicate_clip_to_arrangement(clip, destination_time)

        Available in Live 11 / 12.  destination_time is in beats from the
        start of the arrangement.
        """
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")

            track = self._song.tracks[track_index]

            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip slot index out of range")

            clip_slot = track.clip_slots[clip_index]

            if not clip_slot.has_clip:
                raise Exception(
                    "No clip in slot " + str(clip_index) +
                    " on track " + str(track_index)
                )

            clip = clip_slot.clip

            # Duplicate to arrangement at the requested beat position
            track.duplicate_clip_to_arrangement(clip, float(destination_time))

            return {
                "success": True,
                "track_index": track_index,
                "track_name": track.name,
                "clip_name": clip.name,
                "destination_time": destination_time
            }
        except Exception as e:
            self.log_message("Error duplicating clip to arrangement: " + str(e))
            raise

    def _create_locator(self, name, time_val):
        """Create (or rename) a named locator at the given beat position.

        Uses Live's Song.set_or_delete_cue(), which toggles a cue at the
        current_song_time. We temporarily move the playhead, toggle, then
        restore. If a cue already exists at that time we just rename it
        instead of toggling (which would delete it).
        """
        try:
            song = self._song
            target_time = float(time_val)
            tolerance = 1e-3

            # See if a cue already exists at (or near) the target time
            existing = None
            for cue in song.cue_points:
                if abs(cue.time - target_time) < tolerance:
                    existing = cue
                    break

            original_time = song.current_song_time

            if existing is None:
                # Move playhead, toggle to create, then locate the new cue
                song.current_song_time = target_time
                song.set_or_delete_cue()
                for cue in song.cue_points:
                    if abs(cue.time - target_time) < tolerance:
                        existing = cue
                        break
                # Restore playhead
                try:
                    song.current_song_time = original_time
                except Exception:
                    pass

            if existing is None:
                raise Exception("Failed to create cue at time " + str(target_time))

            if name:
                try:
                    existing.name = str(name)
                except Exception as e:
                    self.log_message("Could not rename locator: " + str(e))

            return {
                "success": True,
                "time": existing.time,
                "name": existing.name,
            }
        except Exception as e:
            self.log_message("Error creating locator: " + str(e))
            raise

    # ── Browser implementations ───────────────────────────────────────────────

    def _get_browser_item(self, uri, path):
        """Get a browser item by URI or path"""
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            result = {
                "uri": uri,
                "path": path,
                "found": False
            }
            
            # Try to find by URI first if provided
            if uri:
                item = self._find_browser_item_by_uri(app.browser, uri)
                if item:
                    result["found"] = True
                    result["item"] = {
                        "name": item.name,
                        "is_folder": item.is_folder,
                        "is_device": item.is_device,
                        "is_loadable": item.is_loadable,
                        "uri": item.uri
                    }
                    return result
            
            # If URI not provided or not found, try by path
            if path:
                # Parse the path and navigate to the specified item
                path_parts = path.split("/")
                
                # Determine the root based on the first part
                current_item = None
                if path_parts[0].lower() == "instruments":
                    current_item = app.browser.instruments
                elif path_parts[0].lower() == "sounds":
                    current_item = app.browser.sounds
                elif path_parts[0].lower() == "drums":
                    current_item = app.browser.drums
                elif path_parts[0].lower() == "audio_effects":
                    current_item = app.browser.audio_effects
                elif path_parts[0].lower() == "midi_effects":
                    current_item = app.browser.midi_effects
                else:
                    # Default to instruments if not specified
                    current_item = app.browser.instruments
                    # Don't skip the first part in this case
                    path_parts = ["instruments"] + path_parts
                
                # Navigate through the path
                for i in range(1, len(path_parts)):
                    part = path_parts[i]
                    if not part:  # Skip empty parts
                        continue
                    
                    found = False
                    for child in current_item.children:
                        if child.name.lower() == part.lower():
                            current_item = child
                            found = True
                            break
                    
                    if not found:
                        result["error"] = "Path part '{0}' not found".format(part)
                        return result
                
                # Found the item
                result["found"] = True
                result["item"] = {
                    "name": current_item.name,
                    "is_folder": current_item.is_folder,
                    "is_device": current_item.is_device,
                    "is_loadable": current_item.is_loadable,
                    "uri": current_item.uri
                }
            
            return result
        except Exception as e:
            self.log_message("Error getting browser item: " + str(e))
            self.log_message(traceback.format_exc())
            raise   
    
    
    
    def _load_instrument_or_effect(self, track_index, uri):
        """Load an instrument or effect onto a track by its browser URI.

        The command dispatcher above calls this method, but it was never
        defined — and "load_instrument_or_effect" was missing from the list of
        main-thread commands as well, so the command fell through to the final
        "Unknown command" branch. Loading a device is exactly what
        _load_browser_item does, so delegate to it; the only difference is the
        parameter name the MCP server uses ("uri" vs "item_uri").
        """
        return self._load_browser_item(track_index, uri)

    def _load_browser_item(self, track_index, item_uri):
        """Load a browser item onto a track by its URI"""
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            
            track = self._song.tracks[track_index]
            
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            
            # Find the browser item by URI
            item = self._find_browser_item_by_uri(app.browser, item_uri)
            
            if not item:
                raise ValueError("Browser item with URI '{0}' not found".format(item_uri))
            
            # Select the track
            self._song.view.selected_track = track
            
            # Load the item
            app.browser.load_item(item)
            
            result = {
                "loaded": True,
                "item_name": item.name,
                "track_name": track.name,
                "uri": item_uri
            }
            return result
        except Exception as e:
            self.log_message("Error loading browser item: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    # Substring markers that point a URI at a likely root. Unmatched URIs fall
    # back to the default search order.
    _URI_ROOT_HINTS = (
        ('plugins',       ('vst:', 'vst3:', 'au:', 'query:plugins', 'plugin#')),
        ('max_for_live',  ('max for live', 'maxforlive', 'm4l', 'query:max')),
        ('user_library',  ('user library', 'userlibrary', 'query:user library', 'query:user-library')),
        ('packs',         ('query:packs', '/packs/')),
        ('samples',       ('query:samples', 'sample:', '/samples/')),
        ('drums',         ('query:drums', '/drums/')),
        ('instruments',   ('query:instruments', '/instruments/')),
        ('sounds',        ('query:sounds', '/sounds/')),
        ('audio_effects', ('query:audio effects', 'audioeffects', '/audio_effects/')),
        ('midi_effects',  ('query:midi effects', 'midieffects', '/midi_effects/')),
    )

    def _order_roots_by_uri(self, roots, uri):
        """Reorder ``roots`` so the URI's likely root is walked first."""
        if not isinstance(uri, (bytes, str)) or not uri:
            return roots
        lowered = uri.lower()
        for attr, markers in self._URI_ROOT_HINTS:
            if any(m in lowered for m in markers):
                head = [(a, r) for (a, r) in roots if a == attr]
                tail = [(a, r) for (a, r) in roots if a != attr]
                return head + tail
        return roots

    def _find_browser_item_by_uri(self, browser_or_item, uri, max_depth=10, current_depth=0):
        """Find a browser item by its URI.

        Top-level lookups are memoised on ``self._uri_cache`` so repeated
        loads of the same URI don't re-walk the entire browser tree.
        """
        if current_depth == 0:
            cache = getattr(self, '_uri_cache', None)
            if cache is None:
                self._uri_cache = cache = {}
            if uri in cache:
                return cache[uri]
            result = self._walk_browser_for_uri(browser_or_item, uri, max_depth, 0)
            if result is not None:
                cache[uri] = result
            return result
        return self._walk_browser_for_uri(browser_or_item, uri, max_depth, current_depth)

    def _walk_browser_for_uri(self, browser_or_item, uri, max_depth, current_depth):
        """Recursive walk used by :py:meth:`_find_browser_item_by_uri`."""
        try:
            # Check if this is the item we're looking for
            if hasattr(browser_or_item, 'uri') and browser_or_item.uri == uri:
                return browser_or_item

            # Stop recursion if we've reached max depth
            if current_depth >= max_depth:
                return None

            # Check if this is a browser with root categories
            if hasattr(browser_or_item, 'instruments'):
                roots = [
                    ('instruments', browser_or_item.instruments),
                    ('sounds', browser_or_item.sounds),
                    ('drums', browser_or_item.drums),
                    ('audio_effects', browser_or_item.audio_effects),
                    ('midi_effects', browser_or_item.midi_effects),
                ]
                for extra_attr in ('plugins', 'max_for_live', 'user_library', 'packs', 'samples'):
                    if hasattr(browser_or_item, extra_attr):
                        try:
                            roots.append((extra_attr, getattr(browser_or_item, extra_attr)))
                        except (AttributeError, RuntimeError) as e:
                            self.log_message("Could not access browser.{0}: {1}".format(extra_attr, str(e)))

                for _attr, category in self._order_roots_by_uri(roots, uri):
                    item = self._find_browser_item_by_uri(category, uri, max_depth, current_depth + 1)
                    if item:
                        return item

                return None

            # Check if this item has children
            if hasattr(browser_or_item, 'children') and browser_or_item.children:
                for child in browser_or_item.children:
                    item = self._find_browser_item_by_uri(child, uri, max_depth, current_depth + 1)
                    if item:
                        return item

            return None
        except Exception as e:
            self.log_message("Error finding browser item by URI: {0}".format(str(e)))
            return None
    
    # Helper methods

    def _find_blend_parameter(self, device):
        """Find Dry/Wet, Mix, or Amount on a device for Magnitude mapping."""
        preferred = ("Dry/Wet", "Dry Wet", "Mix", "Amount")
        by_name = {}
        for param in device.parameters:
            try:
                by_name[param.name] = param
            except Exception:
                continue
        for name in preferred:
            if name in by_name:
                return by_name[name], name
        # Case-insensitive fallback
        lowered = dict((k.lower(), (v, k)) for k, v in by_name.items())
        for name in preferred:
            hit = lowered.get(name.lower())
            if hit:
                return hit[0], hit[1]
        return None, None

    def _inspect_rack(self, track_index, device_index=0):
        """Inspect a rack's nested devices and blend parameters."""
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index out of range")
        track = self._song.tracks[track_index]
        if device_index < 0 or device_index >= len(track.devices):
            raise IndexError("Device index out of range")
        rack = track.devices[device_index]
        if not getattr(rack, "can_have_chains", False):
            raise ValueError("Device '{0}' is not a rack".format(rack.name))

        devices_info = []
        for chain_index, chain in enumerate(rack.chains):
            for nested in chain.devices:
                blend, blend_name = self._find_blend_parameter(nested)
                param_names = []
                try:
                    param_names = [p.name for p in nested.parameters]
                except Exception:
                    pass
                devices_info.append({
                    "chain_index": chain_index,
                    "name": nested.name,
                    "class_name": nested.class_name,
                    "blend_param": blend_name,
                    "parameters": param_names,
                })

        return {
            "track_index": track_index,
            "device_index": device_index,
            "rack_name": rack.name,
            "has_macro_map": hasattr(rack, "macro_map"),
            "has_rename_macro": hasattr(rack, "rename_macro"),
            "macros_mapped": list(getattr(rack, "macros_mapped", [])),
            "devices": devices_info,
        }

    def _map_rack_magnitude(self, track_index, device_index=0, macro_name="Magnitude"):
        """Rename Macro 1 and map nested Dry/Wet (or Mix/Amount) params to it."""
        if track_index < 0 or track_index >= len(self._song.tracks):
            raise IndexError("Track index out of range")
        track = self._song.tracks[track_index]
        if device_index < 0 or device_index >= len(track.devices):
            raise IndexError("Device index out of range")
        rack = track.devices[device_index]
        if not getattr(rack, "can_have_chains", False):
            raise ValueError("Device '{0}' is not a rack".format(rack.name))
        if not hasattr(rack, "macro_map"):
            raise RuntimeError(
                "RackDevice.macro_map is unavailable in this Live version")

        # Ensure at least one macro is visible
        try:
            visible = int(getattr(rack, "visible_macro_count", 1) or 1)
            while visible < 1 and hasattr(rack, "add_macro"):
                rack.add_macro()
                visible = int(rack.visible_macro_count)
        except Exception as e:
            self.log_message("Could not adjust visible macros: {0}".format(e))

        if hasattr(rack, "rename_macro"):
            rack.rename_macro(0, macro_name)
        else:
            # Fallback: Macro 1 is usually parameters[1] (0 = Device On)
            try:
                if len(rack.parameters) > 1:
                    rack.parameters[1].name = macro_name
            except Exception:
                pass

        mapped = []
        skipped = []
        for chain_index, chain in enumerate(rack.chains):
            for nested in chain.devices:
                blend, blend_name = self._find_blend_parameter(nested)
                if not blend:
                    skipped.append({
                        "device": nested.name,
                        "reason": "no Dry/Wet, Mix, or Amount parameter",
                    })
                    continue
                try:
                    rack.macro_map(0, blend)
                    mapped.append({
                        "device": nested.name,
                        "parameter": blend_name,
                        "chain_index": chain_index,
                    })
                except Exception as e:
                    skipped.append({
                        "device": nested.name,
                        "parameter": blend_name,
                        "reason": str(e),
                    })

        return {
            "rack_name": rack.name,
            "macro_name": macro_name,
            "macro_index": 0,
            "mapped": mapped,
            "skipped": skipped,
            "macros_mapped": list(getattr(rack, "macros_mapped", [])),
        }
    
    def _get_device_type(self, device):
        """Get the type of a device"""
        try:
            # Simple heuristic - in a real implementation you'd look at the device class
            if device.can_have_drum_pads:
                return "drum_machine"
            elif device.can_have_chains:
                return "rack"
            elif "instrument" in device.class_display_name.lower():
                return "instrument"
            elif "audio_effect" in device.class_name.lower():
                return "audio_effect"
            elif "midi_effect" in device.class_name.lower():
                return "midi_effect"
            else:
                return "unknown"
        except:
            return "unknown"

    # ── Passive human-UI listeners ──────────────────────────────────────────────

    def _enqueue_passive(self, event_type, detail=None, track_index=None, clip_index=None):
        """Append a coarse human-UI event (capped FIFO)."""
        evt = {
            "type": event_type,
            "ts": time.time(),
            "track_index": track_index,
            "clip_index": clip_index,
            "detail": detail if detail is not None else {},
        }
        with self._passive_lock:
            self._passive_events.append(evt)
            if len(self._passive_events) > self._passive_max:
                self._passive_events = self._passive_events[-self._passive_max:]

    def _drain_passive_events(self):
        """Return and clear the passive event queue (called by MCP poller)."""
        with self._passive_lock:
            events = list(self._passive_events)
            self._passive_events = []
        return {"events": events, "count": len(events)}

    def _safe_add_listener(self, obj, add_name, callback):
        try:
            if obj is not None and hasattr(obj, add_name):
                getattr(obj, add_name)(callback)
                return True
        except Exception as e:
            self.log_message("add listener %s failed: %s" % (add_name, str(e)))
        return False

    def _safe_remove_listener(self, obj, remove_name, callback):
        try:
            if obj is not None and hasattr(obj, remove_name):
                getattr(obj, remove_name)(callback)
        except Exception:
            pass

    def _setup_passive_listeners(self):
        """Register high-signal LOM listeners for Mode C / assisted human edits."""
        song = self._song

        def on_tempo():
            try:
                self._enqueue_passive("tempo_changed", {"tempo": float(song.tempo)})
            except Exception:
                self._enqueue_passive("tempo_changed")

        def on_sig_num():
            try:
                self._enqueue_passive(
                    "time_signature_changed",
                    {
                        "signature_numerator": int(song.signature_numerator),
                        "signature_denominator": int(song.signature_denominator),
                    },
                )
            except Exception:
                self._enqueue_passive("time_signature_changed")

        def on_sig_den():
            on_sig_num()

        def on_is_playing():
            try:
                self._enqueue_passive(
                    "playback_changed",
                    {"is_playing": bool(song.is_playing)},
                )
            except Exception:
                self._enqueue_passive("playback_changed")

        def on_tracks():
            count = len(song.tracks)
            previous = getattr(self, "_passive_track_count", None)
            self._passive_track_count = count
            detail = {"track_count": count}
            if previous is not None:
                detail["previous_count"] = previous
                detail["removed"] = count < previous
                detail["added"] = count > previous
            self._enqueue_passive("tracks_changed", detail)
            try:
                self._rebind_track_listeners()
            except Exception as e:
                self.log_message("rebind track listeners failed: " + str(e))

        self._safe_add_listener(song, "add_tempo_listener", on_tempo)
        self._safe_add_listener(song, "add_signature_numerator_listener", on_sig_num)
        self._safe_add_listener(song, "add_signature_denominator_listener", on_sig_den)
        self._safe_add_listener(song, "add_is_playing_listener", on_is_playing)
        self._safe_add_listener(song, "add_tracks_listener", on_tracks)

        self._song_passive_callbacks = [
            ("tempo_listener", on_tempo),
            ("signature_numerator_listener", on_sig_num),
            ("signature_denominator_listener", on_sig_den),
            ("is_playing_listener", on_is_playing),
            ("tracks_listener", on_tracks),
        ]

        self._rebind_track_listeners()
        self.log_message("Passive LOM listeners registered")

    def _teardown_passive_listeners(self):
        song = getattr(self, "_song", None)
        for suffix, cb in getattr(self, "_song_passive_callbacks", []):
            self._safe_remove_listener(song, "remove_" + suffix, cb)
        self._clear_track_listeners()

    def _clear_track_listeners(self):
        for track, bindings in getattr(self, "_passive_track_bindings", []):
            for add_name, callback in bindings:
                remove_name = "remove_" + add_name[len("add_"):]
                target = getattr(callback, "_passive_target", None)
                if target is not None:
                    self._safe_remove_listener(target, remove_name, callback)
                else:
                    self._safe_remove_listener(track, remove_name, callback)
        self._passive_track_bindings = []

    def _rebind_track_listeners(self):
        self._clear_track_listeners()
        song = self._song
        for track_index, track in enumerate(song.tracks):
            bindings = []

            def make_track_cb(kind, t_index):
                def _cb():
                    detail = {}
                    try:
                        t = song.tracks[t_index]
                        if kind == "name_changed":
                            detail["name"] = t.name
                        elif kind == "mute_changed":
                            detail["mute"] = bool(t.mute)
                        elif kind == "solo_changed":
                            detail["solo"] = bool(t.solo)
                        elif kind == "arm_changed":
                            detail["arm"] = self._safe_arm(t)
                        elif kind == "devices_changed":
                            detail["device_count"] = len(t.devices)
                        elif kind == "volume_changed":
                            detail["volume"] = float(t.mixer_device.volume.value)
                        elif kind == "panning_changed":
                            detail["panning"] = float(t.mixer_device.panning.value)
                    except Exception:
                        pass
                    self._enqueue_passive(kind, detail, track_index=t_index)
                return _cb

            pairs = [
                ("add_name_listener", "name_changed"),
                ("add_mute_listener", "mute_changed"),
                ("add_solo_listener", "solo_changed"),
                ("add_arm_listener", "arm_changed"),
                ("add_devices_listener", "devices_changed"),
            ]
            for add_name, kind in pairs:
                cb = make_track_cb(kind, track_index)
                if self._safe_add_listener(track, add_name, cb):
                    bindings.append((add_name, cb))

            try:
                mixer = track.mixer_device
                vol_cb = make_track_cb("volume_changed", track_index)
                vol_cb._passive_target = mixer.volume
                if self._safe_add_listener(mixer.volume, "add_value_listener", vol_cb):
                    bindings.append(("add_value_listener", vol_cb))
                pan_cb = make_track_cb("panning_changed", track_index)
                pan_cb._passive_target = mixer.panning
                if self._safe_add_listener(mixer.panning, "add_value_listener", pan_cb):
                    bindings.append(("add_value_listener", pan_cb))
            except Exception as e:
                self.log_message("mixer listeners failed on track %d: %s" % (track_index, str(e)))

            try:
                for clip_index, slot in enumerate(track.clip_slots):
                    def make_slot_cb(t_index, c_index):
                        def _cb():
                            has = False
                            try:
                                has = bool(song.tracks[t_index].clip_slots[c_index].has_clip)
                            except Exception:
                                pass
                            self._enqueue_passive(
                                "clip_slot_changed",
                                {"has_clip": has},
                                track_index=t_index,
                                clip_index=c_index,
                            )
                            try:
                                self._bind_clip_listeners(t_index, c_index)
                            except Exception:
                                pass
                        return _cb

                    slot_cb = make_slot_cb(track_index, clip_index)
                    slot_cb._passive_target = slot
                    if self._safe_add_listener(slot, "add_has_clip_listener", slot_cb):
                        bindings.append(("add_has_clip_listener", slot_cb))
                    if slot.has_clip:
                        self._bind_clip_listeners(track_index, clip_index, bindings)
            except Exception as e:
                self.log_message("clip slot listeners failed on track %d: %s" % (track_index, str(e)))

            self._passive_track_bindings.append((track, bindings))

    def _bind_clip_listeners(self, track_index, clip_index, bindings=None):
        try:
            track = self._song.tracks[track_index]
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                return
            clip = slot.clip

            def on_name():
                name = ""
                try:
                    name = clip.name
                except Exception:
                    pass
                self._enqueue_passive(
                    "clip_name_changed",
                    {"name": name},
                    track_index=track_index,
                    clip_index=clip_index,
                )

            def on_notes():
                self._enqueue_passive(
                    "clip_notes_changed",
                    {},
                    track_index=track_index,
                    clip_index=clip_index,
                )

            def on_playing():
                playing = False
                try:
                    playing = bool(clip.is_playing)
                except Exception:
                    pass
                self._enqueue_passive(
                    "clip_playing_changed",
                    {"is_playing": playing},
                    track_index=track_index,
                    clip_index=clip_index,
                )

            for add_name, cb in [
                ("add_name_listener", on_name),
                ("add_notes_listener", on_notes),
                ("add_playing_status_listener", on_playing),
            ]:
                cb._passive_target = clip
                if self._safe_add_listener(clip, add_name, cb):
                    if bindings is not None:
                        bindings.append((add_name, cb))
        except Exception as e:
            self.log_message(
                "bind clip listeners %d/%d failed: %s"
                % (track_index, clip_index, str(e))
            )

    # ── Dataset / state snapshot helpers ──────────────────────────────────────

    def _safe_attr(self, obj, attr, cast=None, default=None):
        try:
            val = getattr(obj, attr)
            if callable(val):
                return default
            if cast is not None:
                return cast(val)
            return val
        except Exception:
            return default

    def _notes_from_clip(self, clip):
        """Extract MIDI notes from a clip (incl. MPE/expression when available)."""
        notes = []
        if not clip or not getattr(clip, "is_midi_clip", False):
            return notes

        if hasattr(clip, "get_notes_extended"):
            try:
                raw = clip.get_notes_extended(0, 128, 0.0, float(clip.length) + 1.0)
                for n in raw:
                    entry = {
                        "pitch": int(getattr(n, "pitch", 0)),
                        "start_time": float(getattr(n, "start_time", 0.0)),
                        "duration": float(getattr(n, "duration", 0.0)),
                        "velocity": float(getattr(n, "velocity", 0)),
                        "mute": bool(getattr(n, "mute", False)),
                    }
                    for opt, caster in [
                        ("probability", float),
                        ("velocity_deviation", float),
                        ("release_velocity", float),
                        ("note_id", int),
                    ]:
                        if hasattr(n, opt):
                            try:
                                entry[opt] = caster(getattr(n, opt))
                            except Exception:
                                pass
                    for opt in ("pitch_bend_range", "pressure", "timbre", "slide"):
                        if hasattr(n, opt):
                            try:
                                entry[opt] = float(getattr(n, opt))
                            except Exception:
                                pass
                    notes.append(entry)
                return notes
            except Exception as e:
                self.log_message("get_notes_extended failed, falling back: " + str(e))

        if hasattr(clip, "get_notes"):
            try:
                raw = clip.get_notes(0.0, 0, float(clip.length) + 1.0, 128)
                for n in raw:
                    notes.append({
                        "pitch": int(n[0]),
                        "start_time": float(n[1]),
                        "duration": float(n[2]),
                        "velocity": float(n[3]),
                        "mute": bool(n[4]) if len(n) > 4 else False,
                    })
            except Exception as e:
                self.log_message("get_notes failed: " + str(e))
        return notes

    def _warp_markers_from_clip(self, clip):
        markers = []
        try:
            raw = getattr(clip, "warp_markers", None)
            if not raw:
                return markers
            for m in raw:
                markers.append({
                    "beat_time": float(getattr(m, "beat_time", getattr(m, "time", 0.0))),
                    "sample_time": float(
                        getattr(m, "sample_time", getattr(m, "time", 0.0))
                    ),
                })
        except Exception as e:
            self.log_message("warp_markers read failed: " + str(e))
        return markers

    def _automated_params_for_device(self, device):
        automated = []
        try:
            for param in device.parameters:
                is_auto = False
                try:
                    if hasattr(param, "automation_state"):
                        is_auto = int(param.automation_state) != 0
                    elif hasattr(param, "is_automated"):
                        is_auto = bool(param.is_automated)
                except Exception:
                    continue
                if is_auto:
                    automated.append(param.name)
        except Exception:
            pass
        return automated

    # Racks nest, and a pathological project could nest deeply. Cap the walk so
    # a snapshot can never blow the stack or the payload size.
    _MAX_CHAIN_DEPTH = 4

    def _serialize_device(self, device, device_index, include_params=True, depth=0):
        info = {
            "index": device_index,
            "name": device.name,
            "class_name": device.class_name,
            "type": self._get_device_type(device),
        }
        automated = self._automated_params_for_device(device)
        if automated:
            info["automated_parameters"] = automated
            info["automation_enabled"] = True
        else:
            info["automation_enabled"] = False

        if include_params:
            params = []
            try:
                for p_index, param in enumerate(device.parameters):
                    try:
                        entry = {
                            "index": p_index,
                            "name": param.name,
                            "value": float(param.value),
                            "min": float(param.min),
                            "max": float(param.max),
                            "is_enabled": bool(getattr(param, "is_enabled", True)),
                            "is_quantized": bool(getattr(param, "is_quantized", False)),
                        }
                        if hasattr(param, "value_string"):
                            entry["value_string"] = str(param.value_string)
                        if hasattr(param, "automation_state"):
                            try:
                                entry["automation_state"] = int(param.automation_state)
                            except Exception:
                                pass
                        params.append(entry)
                    except Exception:
                        continue
            except Exception as e:
                self.log_message("Error reading device parameters: " + str(e))
            info["parameters"] = params

        # Devices inside a rack carry the actual sound design — a drum rack's
        # nested Operator, an instrument rack's filter. Without this walk a rack
        # contributes only its 8 macros and the timbral state is invisible.
        if getattr(device, "can_have_chains", False):
            if depth >= self._MAX_CHAIN_DEPTH:
                info["chains_truncated"] = True
            else:
                info["chains"] = self._serialize_chains(
                    device, include_params=include_params, depth=depth
                )
        return info

    def _serialize_chains(self, rack, include_params=True, depth=0):
        chains = []
        try:
            chain_lists = [("chains", getattr(rack, "chains", []))]
            returns = getattr(rack, "return_chains", None)
            if returns:
                chain_lists.append(("return_chains", returns))

            for kind, chain_list in chain_lists:
                for chain_index, chain in enumerate(chain_list):
                    entry = {
                        "index": chain_index,
                        "kind": kind,
                        "chain_name": self._safe_attr(chain, "name", str, ""),
                        "mute": bool(self._safe_attr(chain, "mute", bool, False)),
                        "solo": bool(self._safe_attr(chain, "solo", bool, False)),
                    }
                    try:
                        mixer = chain.mixer_device
                        entry["volume"] = float(mixer.volume.value)
                        entry["panning"] = float(mixer.panning.value)
                    except Exception:
                        pass

                    # Drum racks expose the pad's note, which is what ties a
                    # nested device back to the kick/snare/hat it voices.
                    note = self._safe_attr(chain, "out_note", int, None)
                    if note is not None:
                        entry["out_note"] = note

                    nested = []
                    try:
                        for d_i, dev in enumerate(chain.devices):
                            nested.append(
                                self._serialize_device(
                                    dev,
                                    d_i,
                                    include_params=include_params,
                                    depth=depth + 1,
                                )
                            )
                    except Exception as e:
                        self.log_message("Error reading chain devices: " + str(e))
                    entry["devices"] = nested
                    chains.append(entry)
        except Exception as e:
            self.log_message("Error serializing rack chains: " + str(e))
        return chains

    def _serialize_clip_common(self, clip):
        info = {
            "looping": bool(self._safe_attr(clip, "looping", bool, False)),
            "loop_start": self._safe_attr(clip, "loop_start", float, None),
            "loop_end": self._safe_attr(clip, "loop_end", float, None),
            "start_marker": self._safe_attr(clip, "start_marker", float, None),
            "end_marker": self._safe_attr(clip, "end_marker", float, None),
            "warping": bool(self._safe_attr(clip, "warping", bool, False)),
            "warp_mode": self._safe_attr(clip, "warp_mode", int, None),
            "gain": self._safe_attr(clip, "gain", float, None),
            "pitch_coarse": self._safe_attr(clip, "pitch_coarse", int, None),
            "pitch_fine": self._safe_attr(clip, "pitch_fine", int, None),
            "launch_mode": self._safe_attr(clip, "launch_mode", int, None),
        }
        for attr in ("file_path", "file_path_relative"):
            path = self._safe_attr(clip, attr, str, None)
            if path:
                info["file_path"] = path
                break
        markers = self._warp_markers_from_clip(clip)
        if markers:
            info["warp_markers"] = markers
            info["warp_marker_count"] = len(markers)
        return dict((k, v) for k, v in info.items() if v is not None)

    def _serialize_session_clip(self, clip, include_notes=True):
        info = {
            "name": clip.name,
            "is_playing": bool(clip.is_playing),
            "is_recording": bool(getattr(clip, "is_recording", False)),
            "is_midi_clip": bool(getattr(clip, "is_midi_clip", False)),
            "is_audio_clip": bool(getattr(clip, "is_audio_clip", False)),
            "color": int(getattr(clip, "color", 0)),
        }
        info.update(self._length_report(clip))
        info.update(self._serialize_clip_common(clip))
        if include_notes and info["is_midi_clip"]:
            info["notes"] = self._notes_from_clip(clip)
            info["note_count"] = len(info["notes"])
        return info

    def _serialize_arrangement_clip(self, clip, include_notes=True):
        info = {
            "name": clip.name,
            "start_time": float(clip.start_time),
            "end_time": float(clip.end_time),
            "length": float(clip.length),
            "color": int(getattr(clip, "color", 0)),
            "is_midi_clip": bool(getattr(clip, "is_midi_clip", False)),
            "is_audio_clip": bool(getattr(clip, "is_audio_clip", False)),
            "is_playing": bool(getattr(clip, "is_playing", False)),
        }
        info.update(self._length_report(clip))
        info.update(self._serialize_clip_common(clip))
        if include_notes and info["is_midi_clip"]:
            info["notes"] = self._notes_from_clip(clip)
            info["note_count"] = len(info["notes"])
        return info

    def _serialize_sends(self, track):
        sends = []
        try:
            for i, send in enumerate(track.mixer_device.sends):
                sends.append({
                    "index": i,
                    "value": float(send.value),
                    "name": str(getattr(send, "name", "Send %d" % i)),
                })
        except Exception:
            pass
        return sends

    def _serialize_scenes(self):
        scenes = []
        try:
            for i, scene in enumerate(self._song.scenes):
                scenes.append({
                    "index": i,
                    "name": str(scene.name),
                    "tempo": self._safe_attr(scene, "tempo", float, None),
                    "is_triggered": bool(self._safe_attr(scene, "is_triggered", bool, False)),
                })
        except Exception as e:
            self.log_message("scenes serialize failed: " + str(e))
        return scenes

    def _serialize_cue_points(self):
        cues = []
        try:
            for cue in self._song.cue_points:
                cues.append({
                    "name": str(getattr(cue, "name", "")),
                    "time": float(getattr(cue, "time", 0.0)),
                })
        except Exception as e:
            self.log_message("cue_points serialize failed: " + str(e))
        return cues

    def _serialize_return_tracks(self, include_params=True):
        returns = []
        try:
            for i, track in enumerate(self._song.return_tracks):
                devices = []
                for d_i, device in enumerate(track.devices):
                    devices.append(
                        self._serialize_device(device, d_i, include_params=include_params)
                    )
                returns.append({
                    "index": i,
                    "name": track.name,
                    "mute": bool(track.mute),
                    "solo": bool(track.solo),
                    "volume": float(track.mixer_device.volume.value),
                    "panning": float(track.mixer_device.panning.value),
                    "devices": devices,
                })
        except Exception as e:
            self.log_message("return_tracks serialize failed: " + str(e))
        return returns

    def _serialize_master_track(self, include_params=True):
        """Master chain — the bus compressor/limiter that shapes the final sound."""
        try:
            track = self._song.master_track
            devices = []
            for d_i, device in enumerate(track.devices):
                devices.append(
                    self._serialize_device(device, d_i, include_params=include_params)
                )
            return {
                "volume": float(track.mixer_device.volume.value),
                "panning": float(track.mixer_device.panning.value),
                "devices": devices,
            }
        except Exception as e:
            self.log_message("master_track serialize failed: " + str(e))
            return None

    def _get_clip_notes(self, track_index, clip_index):
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            track = self._song.tracks[track_index]
            if clip_index < 0 or clip_index >= len(track.clip_slots):
                raise IndexError("Clip index out of range")
            slot = track.clip_slots[clip_index]
            if not slot.has_clip:
                raise Exception("No clip in slot")
            clip = slot.clip
            if not getattr(clip, "is_midi_clip", False):
                raise Exception("Clip is not a MIDI clip")
            notes = self._notes_from_clip(clip)
            return {
                "track_index": track_index,
                "clip_index": clip_index,
                "clip_name": clip.name,
                "length": float(clip.length),
                "note_count": len(notes),
                "notes": notes,
            }
        except Exception as e:
            self.log_message("Error getting clip notes: " + str(e))
            raise

    def _get_device_parameters(self, track_index, device_index):
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            track = self._song.tracks[track_index]
            if device_index < 0 or device_index >= len(track.devices):
                raise IndexError("Device index out of range")
            device = track.devices[device_index]
            return {
                "track_index": track_index,
                "device": self._serialize_device(device, device_index, include_params=True),
            }
        except Exception as e:
            self.log_message("Error getting device parameters: " + str(e))
            raise

    def _get_session_snapshot(self, include_notes=True, include_params=True):
        """Full v2 project state dump for trajectory dataset recording."""
        try:
            session = self._get_session_info()
            tracks = []
            for track_index, track in enumerate(self._song.tracks):
                clip_slots = []
                for slot_index, slot in enumerate(track.clip_slots):
                    clip_info = None
                    if slot.has_clip:
                        clip_info = self._serialize_session_clip(
                            slot.clip, include_notes=include_notes
                        )
                    clip_slots.append({
                        "index": slot_index,
                        "has_clip": bool(slot.has_clip),
                        "clip": clip_info,
                    })

                devices = []
                for device_index, device in enumerate(track.devices):
                    devices.append(
                        self._serialize_device(
                            device, device_index, include_params=include_params
                        )
                    )

                arrangement_clips = []
                try:
                    for clip in track.arrangement_clips:
                        arrangement_clips.append(
                            self._serialize_arrangement_clip(
                                clip, include_notes=include_notes
                            )
                        )
                except Exception as e:
                    self.log_message(
                        "arrangement_clips unavailable on track %d: %s"
                        % (track_index, str(e))
                    )

                tracks.append({
                    "index": track_index,
                    "name": track.name,
                    "is_audio_track": bool(track.has_audio_input),
                    "is_midi_track": bool(track.has_midi_input),
                    "mute": bool(track.mute),
                    "solo": bool(track.solo),
                    "arm": self._safe_arm(track),
                    "volume": float(track.mixer_device.volume.value),
                    "panning": float(track.mixer_device.panning.value),
                    "sends": self._serialize_sends(track),
                    "clip_slots": clip_slots,
                    "devices": devices,
                    "arrangement_clips": arrangement_clips,
                })

            return {
                "schema": "ableton_mcp_snapshot_v2",
                "session": session,
                "tracks": tracks,
                "scenes": self._serialize_scenes(),
                "return_tracks": self._serialize_return_tracks(
                    include_params=include_params
                ),
                "master_track": self._serialize_master_track(
                    include_params=include_params
                ),
                "cue_points": self._serialize_cue_points(),
                "include_notes": bool(include_notes),
                "include_params": bool(include_params),
            }
        except Exception as e:
            self.log_message("Error getting session snapshot: " + str(e))
            raise

    def _set_device_parameter(self, track_index, device_index, parameter_index, value):
        try:
            if track_index < 0 or track_index >= len(self._song.tracks):
                raise IndexError("Track index out of range")
            track = self._song.tracks[track_index]
            if device_index < 0 or device_index >= len(track.devices):
                raise IndexError("Device index out of range")
            device = track.devices[device_index]
            if parameter_index < 0 or parameter_index >= len(device.parameters):
                raise IndexError("Parameter index out of range")
            param = device.parameters[parameter_index]
            old = float(param.value)
            param.value = float(value)
            return {
                "track_index": track_index,
                "device_index": device_index,
                "parameter_index": parameter_index,
                "name": param.name,
                "old_value": old,
                "value": float(param.value),
                "min": float(param.min),
                "max": float(param.max),
            }
        except Exception as e:
            self.log_message("Error setting device parameter: " + str(e))
            raise

    def get_browser_tree(self, category_type="all"):
        """
        Get a simplified tree of browser categories.
        
        Args:
            category_type: Type of categories to get ('all', 'instruments', 'sounds', etc.)
            
        Returns:
            Dictionary with the browser tree structure
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
            
            result = {
                "type": category_type,
                "categories": [],
                "available_categories": browser_attrs
            }
            
            # The full browser is far too large to serialise, so descend one
            # level and mark anything deeper with has_more. The MCP server
            # renders that as "[...]" and get_browser_items_at_path walks on
            # from there.
            max_depth = 1
            max_children = 64
            folder_count = [0]

            def process_item(item, depth=0):
                if not item:
                    return None

                children = getattr(item, 'children', None) or []
                is_folder = bool(children)
                if is_folder:
                    folder_count[0] += 1

                node = {
                    "name": item.name if hasattr(item, 'name') else "Unknown",
                    "is_folder": is_folder,
                    "is_device": hasattr(item, 'is_device') and item.is_device,
                    "is_loadable": hasattr(item, 'is_loadable') and item.is_loadable,
                    "uri": item.uri if hasattr(item, 'uri') else None,
                    "children": []
                }

                if is_folder and depth >= max_depth:
                    node["has_more"] = True
                    return node

                for child in children[:max_children]:
                    try:
                        processed = process_item(child, depth + 1)
                    except Exception as e:
                        self.log_message("Error processing browser child: {0}".format(str(e)))
                        continue
                    if processed:
                        node["children"].append(processed)
                if len(children) > max_children:
                    node["has_more"] = True

                return node

            # Process based on category type and available attributes
            if (category_type == "all" or category_type == "instruments") and hasattr(app.browser, 'instruments'):
                try:
                    instruments = process_item(app.browser.instruments)
                    if instruments:
                        instruments["name"] = "Instruments"  # Ensure consistent naming
                        result["categories"].append(instruments)
                except Exception as e:
                    self.log_message("Error processing instruments: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "sounds") and hasattr(app.browser, 'sounds'):
                try:
                    sounds = process_item(app.browser.sounds)
                    if sounds:
                        sounds["name"] = "Sounds"  # Ensure consistent naming
                        result["categories"].append(sounds)
                except Exception as e:
                    self.log_message("Error processing sounds: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "drums") and hasattr(app.browser, 'drums'):
                try:
                    drums = process_item(app.browser.drums)
                    if drums:
                        drums["name"] = "Drums"  # Ensure consistent naming
                        result["categories"].append(drums)
                except Exception as e:
                    self.log_message("Error processing drums: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "audio_effects") and hasattr(app.browser, 'audio_effects'):
                try:
                    audio_effects = process_item(app.browser.audio_effects)
                    if audio_effects:
                        audio_effects["name"] = "Audio Effects"  # Ensure consistent naming
                        result["categories"].append(audio_effects)
                except Exception as e:
                    self.log_message("Error processing audio_effects: {0}".format(str(e)))
            
            if (category_type == "all" or category_type == "midi_effects") and hasattr(app.browser, 'midi_effects'):
                try:
                    midi_effects = process_item(app.browser.midi_effects)
                    if midi_effects:
                        midi_effects["name"] = "MIDI Effects"
                        result["categories"].append(midi_effects)
                except Exception as e:
                    self.log_message("Error processing midi_effects: {0}".format(str(e)))
            
            # Try to process other potentially available categories
            for attr in browser_attrs:
                if attr not in ['instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects'] and \
                   (category_type == "all" or category_type == attr):
                    try:
                        item = getattr(app.browser, attr)
                        if hasattr(item, 'children') or hasattr(item, 'name'):
                            category = process_item(item)
                            if category:
                                category["name"] = attr.capitalize()
                                result["categories"].append(category)
                    except Exception as e:
                        self.log_message("Error processing {0}: {1}".format(attr, str(e)))
            
            # The MCP server reports this back to the caller; without it the
            # header always claimed zero folders.
            result["total_folders"] = folder_count[0]

            self.log_message("Browser tree generated for {0} with {1} root categories".format(
                category_type, len(result['categories'])))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser tree: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise
    
    def get_browser_items_at_path(self, path):
        """
        Get browser items at a specific path.
        
        Args:
            path: Path in the format "category/folder/subfolder"
                 where category is one of: instruments, sounds, drums, audio_effects, midi_effects
                 or any other available browser category
                 
        Returns:
            Dictionary with items at the specified path
        """
        try:
            # Access the application's browser instance instead of creating a new one
            app = self.application()
            if not app:
                raise RuntimeError("Could not access Live application")
                
            # Check if browser is available
            if not hasattr(app, 'browser') or app.browser is None:
                raise RuntimeError("Browser is not available in the Live application")
            
            # Log available browser attributes to help diagnose issues
            browser_attrs = [attr for attr in dir(app.browser) if not attr.startswith('_')]
            self.log_message("Available browser attributes: {0}".format(browser_attrs))
                
            # Parse the path
            path_parts = path.split("/")
            if not path_parts:
                raise ValueError("Invalid path")
            
            # Determine the root category
            root_category = path_parts[0].lower()
            current_item = None
            
            # Check standard categories first
            if root_category == "instruments" and hasattr(app.browser, 'instruments'):
                current_item = app.browser.instruments
            elif root_category == "sounds" and hasattr(app.browser, 'sounds'):
                current_item = app.browser.sounds
            elif root_category == "drums" and hasattr(app.browser, 'drums'):
                current_item = app.browser.drums
            elif root_category == "audio_effects" and hasattr(app.browser, 'audio_effects'):
                current_item = app.browser.audio_effects
            elif root_category == "midi_effects" and hasattr(app.browser, 'midi_effects'):
                current_item = app.browser.midi_effects
            else:
                # Try to find the category in other browser attributes
                found = False
                for attr in browser_attrs:
                    if attr.lower() == root_category:
                        try:
                            current_item = getattr(app.browser, attr)
                            found = True
                            break
                        except Exception as e:
                            self.log_message("Error accessing browser attribute {0}: {1}".format(attr, str(e)))
                
                if not found:
                    # If we still haven't found the category, return available categories
                    return {
                        "path": path,
                        "error": "Unknown or unavailable category: {0}".format(root_category),
                        "available_categories": browser_attrs,
                        "items": []
                    }
            
            # Navigate through the path
            for i in range(1, len(path_parts)):
                part = path_parts[i]
                if not part:  # Skip empty parts
                    continue
                
                if not hasattr(current_item, 'children'):
                    return {
                        "path": path,
                        "error": "Item at '{0}' has no children".format('/'.join(path_parts[:i])),
                        "items": []
                    }
                
                found = False
                for child in current_item.children:
                    if hasattr(child, 'name') and child.name.lower() == part.lower():
                        current_item = child
                        found = True
                        break
                
                if not found:
                    return {
                        "path": path,
                        "error": "Path part '{0}' not found".format(part),
                        "items": []
                    }
            
            # Get items at the current path
            items = []
            if hasattr(current_item, 'children'):
                for child in current_item.children:
                    item_info = {
                        "name": child.name if hasattr(child, 'name') else "Unknown",
                        "is_folder": hasattr(child, 'children') and bool(child.children),
                        "is_device": hasattr(child, 'is_device') and child.is_device,
                        "is_loadable": hasattr(child, 'is_loadable') and child.is_loadable,
                        "uri": child.uri if hasattr(child, 'uri') else None
                    }
                    items.append(item_info)
            
            result = {
                "path": path,
                "name": current_item.name if hasattr(current_item, 'name') else "Unknown",
                "uri": current_item.uri if hasattr(current_item, 'uri') else None,
                "is_folder": hasattr(current_item, 'children') and bool(current_item.children),
                "is_device": hasattr(current_item, 'is_device') and current_item.is_device,
                "is_loadable": hasattr(current_item, 'is_loadable') and current_item.is_loadable,
                "items": items
            }
            
            self.log_message("Retrieved {0} items at path: {1}".format(len(items), path))
            return result
            
        except Exception as e:
            self.log_message("Error getting browser items at path: {0}".format(str(e)))
            self.log_message(traceback.format_exc())
            raise

    # ── Recording, mixer, routing, clip region ────────────────────────────────
    # These commands are scheduled onto Live's main thread. Validation finishes
    # before the first Live write. Nothing here calls Clip.crop, deletes a
    # clip, replaces an audio file, or sleeps until a take finishes.

    def _run_on_main_thread(self, task, timeout=10.0):
        """Run task on Live's main thread and return a response dict."""
        response_queue = queue.Queue()

        def main_thread_task():
            try:
                response_queue.put({"status": "success", "result": task()})
            except Exception as e:
                self.log_message("Error in main thread task: " + str(e))
                self.log_message(traceback.format_exc())
                response_queue.put({"status": "error", "message": str(e)})

        try:
            self.schedule_message(0, main_thread_task)
        except AssertionError:
            # schedule_message refuses the call when we are already on the
            # main thread. Run the task directly in that case.
            main_thread_task()

        try:
            return response_queue.get(timeout=timeout)
        except queue.Empty:
            return {
                "status": "error",
                "message": "Timeout waiting for operation to complete",
            }

    def _recording_handlers(self):
        return {
            "get_recording_state": self._cmd_get_recording_state,
            "get_track_mixer_state": self._cmd_get_track_mixer_state,
            "set_track_arm": self._cmd_set_track_arm,
            "set_track_mute": self._cmd_set_track_mute,
            "set_track_solo": self._cmd_set_track_solo,
            "set_track_volume": self._cmd_set_track_volume,
            "set_track_panning": self._cmd_set_track_panning,
            "set_track_send": self._cmd_set_track_send,
            "set_track_monitoring": self._cmd_set_track_monitoring,
            "set_track_input_routing": self._cmd_set_track_input_routing,
            "set_arrangement_record": self._cmd_set_arrangement_record,
            "set_session_record": self._cmd_set_session_record,
            "set_arrangement_overdub": self._cmd_set_arrangement_overdub,
            "record_empty_slot": self._cmd_record_empty_slot,
            "stop_recording": self._cmd_stop_recording,
            "prepare_track": self._cmd_prepare_track,
            "get_clip_region": self._cmd_get_clip_region,
            "set_clip_region": self._cmd_set_clip_region,
            "quick_record": self._cmd_quick_record,
            "quick_arrangement_record": self._cmd_quick_arrangement_record,
            "quick_arrangement_stop": self._cmd_quick_arrangement_stop,
            "set_mixer_batch": self._cmd_set_mixer_batch,
            "record_loop": self._cmd_record_loop,
            "stop_record_loop": self._cmd_stop_record_loop,
            "stop_take": self._cmd_stop_take,
            "set_metronome": self._cmd_set_metronome,
            "set_count_in": self._cmd_set_count_in,
            "create_scene": self._cmd_create_scene,
            "keep_take": self._cmd_keep_take,
            "list_takes": self._cmd_list_takes,
            "get_performance_state": self._cmd_get_performance_state,
            "show_view": self._cmd_show_view,
            "arm_tracks": self._cmd_arm_tracks,
            "take": self._cmd_take,
            "again": self._cmd_again,
            "scrap": self._cmd_scrap,
            "cancel": self._cmd_cancel,
            "play_transport": self._cmd_play_transport,
            "pause_transport": self._cmd_pause_transport,
            "nudge_tempo": self._cmd_nudge_tempo,
            "mark_playhead": self._cmd_mark_playhead,
            "list_take_lanes": self._cmd_list_take_lanes,
            "label_take_lane": self._cmd_label_take_lane,
            "delete_take_lane": self._cmd_delete_take_lane,
            "stop_performance": self._cmd_stop_performance,
            "keep_last": self._cmd_keep_last,
        }

    def _dispatch_recording_command(self, command_type, params):
        if not isinstance(params, dict):
            return {"status": "error", "message": "params must be an object"}
        if command_type == "quick_arrangement_stop":
            try:
                return self._confirmed_arrangement_stop(params)
            except Exception as e:
                return {"status": "error", "message": str(e)}
        handler = self._recording_handlers().get(command_type)
        if handler is None:
            return {
                "status": "error",
                "message": "Unknown command: " + str(command_type),
            }
        return self._run_on_main_thread(lambda: handler(params))

    def _reject_unknown(self, params, allowed):
        unknown = [key for key in params.keys() if key not in allowed]
        if unknown:
            raise ValueError(
                "Unknown fields: %s. Allowed: %s"
                % (", ".join(sorted(str(k) for k in unknown)), ", ".join(sorted(allowed)))
            )

    def _require_present_int(self, params, name):
        if name not in params:
            raise ValueError("%s is required" % name)
        return self._require_int(params[name], name)

    def _require_int(self, value, name):
        # bool is a subclass of int; reject it so true is not treated as 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("%s must be an integer" % name)
        return value

    def _require_bool(self, value, name):
        if not isinstance(value, bool):
            raise ValueError("%s must be a boolean" % name)
        return value

    def _require_present_bool(self, params, name):
        if name not in params:
            raise ValueError("%s is required" % name)
        return self._require_bool(params[name], name)

    def _finite_float(self, value, name):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("%s must be a finite number" % name)
        value = float(value)
        if value != value or value == float("inf") or value == float("-inf"):
            raise ValueError("%s must be a finite number" % name)
        return value

    def _require_record_length(self, value):
        length = self._finite_float(value, "record_length")
        if length <= 0 or length > MAX_EMPTY_SLOT_RECORD_BEATS:
            raise ValueError(
                "record_length must be a positive number of beats up to %d"
                % MAX_EMPTY_SLOT_RECORD_BEATS
            )
        return length

    def _fold_text(self, text):
        try:
            return text.casefold()
        except AttributeError:
            return text.lower()

    def _optional_song_value(self, attr, cast, default):
        """Read a song attribute. Any failure returns default and does not raise."""
        try:
            value = getattr(self._song, attr)
        except Exception:
            return default
        try:
            if cast is int and isinstance(value, bool):
                return default
            return cast(value)
        except Exception:
            return default

    def _read_song_bool(self, attr):
        return self._optional_song_value(attr, bool, False)

    def _track_at(self, track_index):
        try:
            tracks = list(self._song.tracks)
        except Exception as e:
            raise ValueError("Could not read song tracks: %s" % e)
        if track_index < 0 or track_index >= len(tracks):
            raise IndexError("Track index out of range")
        return tracks[track_index]

    def _slot_at(self, track, clip_index):
        try:
            slots = list(track.clip_slots)
        except Exception as e:
            raise ValueError("Could not read clip slots: %s" % e)
        if clip_index < 0 or clip_index >= len(slots):
            raise IndexError("Clip index out of range")
        return slots[clip_index]

    def _is_frozen(self, track):
        try:
            return bool(getattr(track, "is_frozen", False))
        except Exception:
            return False

    def _is_group_track(self, track):
        try:
            return bool(getattr(track, "is_foldable", False))
        except Exception:
            return False

    def _can_be_armed(self, track):
        try:
            return bool(getattr(track, "can_be_armed", False))
        except Exception:
            return False

    def _assert_can_arm(self, track):
        if self._is_frozen(track):
            raise ValueError("Track is frozen; arm was not changed")
        if not self._can_be_armed(track):
            raise ValueError(
                "Track cannot be armed. Group, return, and main tracks have "
                "no arm state. Arm was not changed."
            )

    def _mixer(self, track):
        try:
            mixer = track.mixer_device
        except Exception as e:
            raise ValueError("Track has no mixer device (%s)" % e)
        if mixer is None:
            raise ValueError("Track has no mixer device")
        return mixer

    def _assign_bool_attr(self, obj, attr, value):
        current = bool(getattr(obj, attr))
        if current == value:
            return current, False
        setattr(obj, attr, value)
        return bool(getattr(obj, attr)), True

    def _assign_song_bool(self, attr, value):
        try:
            current = getattr(self._song, attr)
        except AttributeError:
            raise ValueError("%s is not available on this Live song" % attr)
        if bool(current) == value:
            return bool(current), False
        setattr(self._song, attr, value)
        return bool(getattr(self._song, attr)), True

    def _assign_param(self, param, value, label):
        value = self._finite_float(value, label)
        try:
            lo = float(param.min)
            hi = float(param.max)
            current = float(param.value)
        except Exception as e:
            raise ValueError("%s could not be read (%s); value was not set" % (label, e))
        if value < lo or value > hi:
            raise ValueError("%s must be between %s and %s" % (label, lo, hi))
        if current == value:
            return current, False
        param.value = value
        try:
            readback = float(param.value)
        except Exception:
            readback = value
        return readback, True

    def _monitoring_value(self, name):
        if not isinstance(name, str):
            raise ValueError("monitoring must be one of: in, auto, off")
        key = self._fold_text(name.strip())
        if key not in _MONITORING_STATE_BY_NAME:
            raise ValueError("monitoring must be one of: in, auto, off")
        return _MONITORING_STATE_BY_NAME[key]

    def _read_monitoring(self, track):
        try:
            raw = getattr(track, "current_monitoring_state")
            if isinstance(raw, bool) or not isinstance(raw, int):
                return None
            return int(raw)
        except Exception:
            return None

    def _require_monitoring(self, track):
        current = self._read_monitoring(track)
        if current is None:
            raise ValueError("Track has no monitoring state; monitoring was not changed")
        return current

    def _routing_name(self, routing):
        if isinstance(routing, str):
            return routing
        name = None
        try:
            name = getattr(routing, "display_name", None)
        except Exception:
            name = None
        if name is None and isinstance(routing, dict):
            name = routing.get("display_name")
        if not isinstance(name, str) or name == "":
            raise ValueError("Routing option has no display_name")
        return name

    def _routing_options(self, track, attr, label):
        try:
            raw = getattr(track, attr)
            options = list(raw)
        except Exception as e:
            raise ValueError("Track has no %s (%s)" % (label, e))
        return options

    def _routing_names(self, options):
        names = []
        for option in options:
            try:
                names.append(self._routing_name(option))
            except ValueError:
                names.append("?")
        return names

    def _match_routing(self, options, wanted, label):
        if not isinstance(wanted, str) or wanted.strip() == "":
            raise ValueError("%s must be a non-empty string" % label)
        names = self._routing_names(options)
        exact = []
        folded = []
        wanted_fold = self._fold_text(wanted)
        for option, name in zip(options, names):
            if name == "?":
                continue
            if name == wanted:
                exact.append(option)
            elif self._fold_text(name) == wanted_fold:
                folded.append(option)
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise ValueError("%s %r matches more than one routing" % (label, wanted))
        if len(folded) == 1:
            return folded[0]
        shown = ", ".join(names) if names else "(none)"
        if len(folded) > 1:
            raise ValueError("%s %r is ambiguous. Options: %s" % (label, wanted, shown))
        raise ValueError("%s %r is not available. Options: %s" % (label, wanted, shown))

    def _assign_input_routing(self, track, routing_type, routing_channel, channel_name):
        """Set input type, then channel. A failed channel match restores the type."""
        previous_type = None
        type_written = False
        wrote = False
        try:
            if routing_type is not None:
                current_name = self._routing_name(track.input_routing_type)
                if current_name != self._routing_name(routing_type):
                    previous_type = track.input_routing_type
                    track.input_routing_type = routing_type
                    type_written = True
                    wrote = True
            if routing_channel is None and channel_name is not None:
                channels = self._routing_options(
                    track, "available_input_routing_channels", "input routing channels"
                )
                routing_channel = self._match_routing(
                    channels, channel_name, "input routing channel"
                )
            if routing_channel is not None:
                current_channel = self._routing_name(track.input_routing_channel)
                if current_channel != self._routing_name(routing_channel):
                    track.input_routing_channel = routing_channel
                    wrote = True
        except Exception:
            if type_written and previous_type is not None:
                try:
                    track.input_routing_type = previous_type
                except Exception as restore_error:
                    self.log_message(
                        "input routing restore failed: " + str(restore_error)
                    )
            raise
        return wrote

    def _selected_scene_index(self):
        try:
            selected = self._song.view.selected_scene
            scenes = list(self._song.scenes)
        except Exception:
            return None
        for index, scene in enumerate(scenes):
            if scene is selected:
                return index
        for index, scene in enumerate(scenes):
            if scene == selected:
                return index
        return None

    def _playing_slot_index(self, track):
        try:
            value = getattr(track, "playing_slot_index")
        except Exception:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def _slot_or_none(self, track, clip_index):
        try:
            slots = list(track.clip_slots)
        except Exception:
            return None
        if clip_index < 0 or clip_index >= len(slots):
            return None
        return slots[clip_index]

    def _slot_is_recording(self, slot):
        try:
            if bool(getattr(slot, "is_recording", False)):
                return True
        except Exception:
            pass
        try:
            if getattr(slot, "has_clip", False) and bool(slot.clip.is_recording):
                return True
        except Exception:
            pass
        return False

    def _recording_slot_indexes(self, track):
        found = []
        try:
            slots = list(track.clip_slots)
        except Exception:
            return found
        for index, slot in enumerate(slots):
            if self._slot_is_recording(slot):
                found.append(index)
        return found

    def _iter_tracks(self):
        try:
            return list(self._song.tracks)
        except Exception:
            return []

    def _session_record_blockers(self):
        """Clips session record would write into. Reads only."""
        scene_index = self._selected_scene_index()
        conflicts = []
        armed = []
        for track_index, track in enumerate(self._iter_tracks()):
            if not self._safe_arm(track):
                continue
            armed.append(track_index)
            playing = self._playing_slot_index(track)
            reported = set()
            if playing is not None and playing >= 0:
                slot = self._slot_or_none(track, playing)
                if slot is not None and bool(getattr(slot, "has_clip", False)):
                    conflicts.append({
                        "track_index": track_index,
                        "clip_index": playing,
                        "reason": "playing_clip",
                    })
                    reported.add(playing)
            if scene_index is not None and scene_index not in reported:
                slot = self._slot_or_none(track, scene_index)
                if slot is not None and bool(getattr(slot, "has_clip", False)):
                    conflicts.append({
                        "track_index": track_index,
                        "clip_index": scene_index,
                        "reason": "selected_scene_clip",
                    })
        return scene_index, armed, conflicts

    def _arrangement_record_blockers(self):
        """Arrangement clips covering the playhead on armed tracks. Reads only."""
        try:
            song_time = float(self._song.current_song_time)
        except Exception:
            return None, [], ["song_time"]
        conflicts = []
        unverified = []
        for track_index, track in enumerate(self._iter_tracks()):
            if not self._safe_arm(track):
                continue
            try:
                clips = list(track.arrangement_clips)
            except Exception:
                unverified.append(track_index)
                continue
            track_unverified = False
            for clip_index, clip in enumerate(clips):
                try:
                    start = float(clip.start_time)
                    end = float(clip.end_time)
                except Exception:
                    track_unverified = True
                    break
                if start <= song_time < end:
                    conflicts.append({
                        "track_index": track_index,
                        "clip_index": clip_index,
                        "start_time": start,
                        "end_time": end,
                        "song_time": song_time,
                    })
            if track_unverified:
                unverified.append(track_index)
        return song_time, conflicts, unverified

    def _format_slot_conflicts(self, conflicts):
        return "; ".join(
            "track %s slot %s (%s)" % (
                item.get("track_index"), item.get("clip_index"), item.get("reason")
            )
            for item in conflicts
        )

    def _song_transport_snapshot(self):
        status_value = self._optional_song_value("session_record_status", int, None)
        count_in = self._optional_song_value("count_in_duration", int, None)
        return {
            "is_playing": self._optional_song_value("is_playing", bool, None),
            "current_song_time": self._optional_song_value("current_song_time", float, None),
            "record_mode": self._optional_song_value("record_mode", bool, None),
            "session_record": self._optional_song_value("session_record", bool, None),
            "session_record_status": (
                _SESSION_RECORD_STATUS_NAMES.get(status_value, "unknown")
                if status_value is not None else None
            ),
            "session_record_status_value": status_value,
            "arrangement_overdub": self._optional_song_value(
                "arrangement_overdub", bool, None
            ),
            "legacy_overdub": self._optional_song_value("overdub", bool, None),
            "metronome": self._optional_song_value("metronome", bool, None),
            "is_counting_in": self._optional_song_value("is_counting_in", bool, None),
            "count_in_duration": count_in,
            "count_in_label": (
                _COUNT_IN_LABELS.get(count_in, "unknown") if count_in is not None else None
            ),
            "exclusive_arm": self._optional_song_value("exclusive_arm", bool, None),
            "selected_scene_index": self._selected_scene_index(),
        }

    def _length_report(self, clip, recording=None):
        """Beat length, or null while Live is still reporting the take in frames."""
        if clip is None:
            status = "recording" if recording else "empty"
            return {"length": None, "length_status": status}
        if recording is None:
            recording = bool(getattr(clip, "is_recording", False))
        raw = None
        try:
            raw = float(clip.length)
            if raw != raw or raw in (float("inf"), float("-inf")):
                raw = None
        except Exception:
            raw = None
        unsettled = recording and (raw is None or raw > _UNSETTLED_RECORD_LENGTH)
        if unsettled:
            report = {"length": None, "length_status": "recording"}
            if raw is not None:
                report["length_raw"] = raw
            return report
        return {
            "length": raw,
            "length_status": "recording" if recording else "beats",
        }

    def _pick_alias(self, params, names, required, label):
        present = [name for name in names if name in params]
        if not present:
            if required:
                raise ValueError("%s is required (%s)" % (label, " or ".join(names)))
            return None
        value = params[present[0]]
        for name in present[1:]:
            if params[name] != value:
                raise ValueError(
                    "Conflicting %s values in %s" % (label, " and ".join(present))
                )
        return value

    def _input_warnings(self, track, track_index):
        try:
            options = self._routing_options(
                track, "available_input_routing_types", "input routing types"
            )
            names = [name for name in self._routing_names(options) if name != "?"]
        except Exception:
            names = []
        useful = [
            name for name in names
            if self._fold_text(name) not in ("no input", "")
        ]
        if useful:
            return []
        return [(
            "Track %s has no available audio input (Ext. In is missing when "
            "Live has no input device). A fired slot can report triggered "
            "without creating a clip."
        ) % track_index]

    def _transport_probe(self):
        """Non-blocking warning. Does not change transport. One short re-read."""
        playing = self._optional_song_value("is_playing", bool, None)
        t0 = self._optional_song_value("current_song_time", float, None)
        detail = {
            "is_playing": playing,
            "song_time": t0,
            "song_time_after": None,
            "transport_advancing": None,
        }
        warnings = []
        if playing is False:
            detail["transport_advancing"] = False
            warnings.append(
                "Transport is stopped. A fired slot can report triggered "
                "without the playhead moving."
            )
            return warnings, detail
        if playing is True and t0 is not None and abs(float(t0)) < 1e-4:
            detail["transport_advancing"] = None
            warnings.append(
                "is_playing is true and current_song_time is 0. If the "
                "playhead is not moving, a fired slot can report triggered "
                "without creating a clip. This call does not wait to find out."
            )
        return warnings, detail

    def _cmd_get_recording_state(self, params):
        self._reject_unknown(params, ())
        state = self._song_transport_snapshot()
        tracks = []
        recording_slots = []
        warnings = []
        for track_index, track in enumerate(self._iter_tracks()):
            slots = self._recording_slot_indexes(track)
            for clip_index in slots:
                slot = self._slot_or_none(track, clip_index)
                clip = None
                if slot is not None and bool(getattr(slot, "has_clip", False)):
                    clip = slot.clip
                entry = {
                    "track_index": track_index,
                    "clip_index": clip_index,
                }
                entry.update(self._length_report(clip, recording=True))
                recording_slots.append(entry)
            tracks.append({
                "index": track_index,
                "name": getattr(track, "name", ""),
                "can_be_armed": self._can_be_armed(track),
                "arm": self._safe_arm(track),
                "is_frozen": self._is_frozen(track),
                "is_group_track": self._is_group_track(track),
                "playing_slot_index": self._playing_slot_index(track),
                "recording_slots": slots,
            })
        state["tracks"] = tracks
        state["recording_slots"] = recording_slots
        state["armed_tracks"] = [
            item["index"] for item in tracks if item["arm"]
        ]
        state["limitations"] = {
            "count_in_writable": False,
            "crop_implemented": False,
            "replaces_source_files": False,
            "uses_trigger_session_record": False,
            "fixed_length": (
                "record_empty_slot accepts record_length in beats and calls "
                "ClipSlot.fire(record_length) on that empty slot only. "
                "Song.trigger_session_record is not used: it targets an "
                "implicit slot and a second call stops recording and starts "
                "playback. Count-in is reported and not changed."
            ),
            "realtime": False,
        }
        state["legacy_overdub_note"] = (
            "Song.overdub is a legacy alias of session record in the Live 12 "
            "Python API. This script reads it and does not set it. "
            "Arrangement overdub is arrangement_overdub."
        )
        transport_warnings, transport = self._transport_probe()
        warnings.extend(transport_warnings)
        state.update(transport)
        for item in tracks:
            if not item["arm"] and not item["recording_slots"]:
                continue
            track = self._track_at(item["index"])
            warnings.extend(self._input_warnings(track, item["index"]))
        state["warnings"] = warnings
        return state

    def _param_state(self, param):
        try:
            return {
                "value": float(param.value),
                "min": float(param.min),
                "max": float(param.max),
                "name": str(getattr(param, "name", "")),
            }
        except Exception:
            return None

    def _routing_state(self, track, current_attr, available_attr):
        current = None
        try:
            current = self._routing_name(getattr(track, current_attr))
        except Exception:
            current = None
        options = []
        try:
            options = self._routing_names(
                self._routing_options(track, available_attr, available_attr)
            )
        except Exception:
            options = []
        return current, options

    def _mixer_state(self, track_index, track):
        mixer = None
        volume = None
        panning = None
        sends = []
        try:
            mixer = track.mixer_device
            volume = self._param_state(mixer.volume)
            panning = self._param_state(mixer.panning)
            for index, send in enumerate(list(mixer.sends)):
                item = self._param_state(send)
                if item is not None:
                    item["index"] = index
                    sends.append(item)
        except Exception:
            pass
        monitoring = self._read_monitoring(track)
        input_type, input_types = self._routing_state(
            track, "input_routing_type", "available_input_routing_types"
        )
        input_channel, input_channels = self._routing_state(
            track, "input_routing_channel", "available_input_routing_channels"
        )
        output_type, _output_types = self._routing_state(
            track, "output_routing_type", "available_output_routing_types"
        )
        output_channel, _output_channels = self._routing_state(
            track, "output_routing_channel", "available_output_routing_channels"
        )
        return {
            "track_index": track_index,
            "name": getattr(track, "name", ""),
            "has_audio_input": bool(getattr(track, "has_audio_input", False)),
            "has_midi_input": bool(getattr(track, "has_midi_input", False)),
            "can_be_armed": self._can_be_armed(track),
            "arm": self._safe_arm(track),
            "is_frozen": self._is_frozen(track),
            "is_group_track": self._is_group_track(track),
            "mute": bool(getattr(track, "mute", False)),
            "solo": bool(getattr(track, "solo", False)),
            "volume": volume,
            "panning": panning,
            "sends": sends,
            "monitoring": (
                _MONITORING_NAME_BY_STATE.get(monitoring) if monitoring is not None else None
            ),
            "monitoring_value": monitoring,
            "input_routing_type": input_type,
            "input_routing_channel": input_channel,
            "available_input_routing_types": input_types,
            "available_input_routing_channels": input_channels,
            "output_routing_type": output_type,
            "output_routing_channel": output_channel,
        }

    def _cmd_get_track_mixer_state(self, params):
        self._reject_unknown(params, ("track_index",))
        track_index = self._require_present_int(params, "track_index")
        track = self._track_at(track_index)
        return self._mixer_state(track_index, track)

    def _cmd_set_track_arm(self, params):
        self._reject_unknown(params, ("track_index", "armed", "arm"))
        track_index = self._require_present_int(params, "track_index")
        armed = self._require_bool(
            self._pick_alias(params, ("armed", "arm"), True, "arm"), "arm"
        )
        track = self._track_at(track_index)
        self._assert_can_arm(track)
        value, changed = self._assign_bool_attr(track, "arm", armed)
        return {
            "track_index": track_index,
            "arm": value,
            "changed": changed,
            "exclusive_arm": self._optional_song_value("exclusive_arm", bool, None),
        }

    def _cmd_set_track_mute(self, params):
        return self._set_track_flag(params, "mute", "mute")

    def _cmd_set_track_solo(self, params):
        return self._set_track_flag(params, "solo", "solo")

    def _set_track_flag(self, params, attr, param_name):
        self._reject_unknown(params, ("track_index", param_name))
        track_index = self._require_present_int(params, "track_index")
        value = self._require_present_bool(params, param_name)
        track = self._track_at(track_index)
        written, changed = self._assign_bool_attr(track, attr, value)
        return {"track_index": track_index, attr: written, "changed": changed}

    def _cmd_set_track_volume(self, params):
        return self._set_track_mixer_param(
            params, "volume", "volume", ("value", "volume")
        )

    def _cmd_set_track_panning(self, params):
        return self._set_track_mixer_param(
            params, "panning", "panning", ("value", "panning", "pan")
        )

    def _set_track_mixer_param(self, params, attr, label, aliases):
        self._reject_unknown(params, ("track_index",) + tuple(aliases))
        track_index = self._require_present_int(params, "track_index")
        raw = self._pick_alias(params, aliases, True, label)
        # Range is checked before assignment inside _assign_param.
        track = self._track_at(track_index)
        param = getattr(self._mixer(track), attr)
        value, changed = self._assign_param(param, raw, label)
        return {
            "track_index": track_index,
            label: value,
            "changed": changed,
            "min": float(param.min),
            "max": float(param.max),
        }

    def _cmd_set_track_send(self, params):
        self._reject_unknown(params, ("track_index", "send_index", "value"))
        track_index = self._require_present_int(params, "track_index")
        send_index = self._require_present_int(params, "send_index")
        if "value" not in params:
            raise ValueError("value is required")
        track = self._track_at(track_index)
        try:
            sends = list(self._mixer(track).sends)
        except Exception as e:
            raise ValueError("Track sends could not be read (%s)" % e)
        if send_index < 0 or send_index >= len(sends):
            raise ValueError(
                "send_index out of range (track has %d sends)" % len(sends)
            )
        value, changed = self._assign_param(
            sends[send_index], params["value"], "send"
        )
        return {
            "track_index": track_index,
            "send_index": send_index,
            "value": value,
            "changed": changed,
            "min": float(sends[send_index].min),
            "max": float(sends[send_index].max),
        }

    def _cmd_set_track_monitoring(self, params):
        self._reject_unknown(params, ("track_index", "monitoring"))
        track_index = self._require_present_int(params, "track_index")
        if "monitoring" not in params:
            raise ValueError("monitoring is required")
        state = self._monitoring_value(params["monitoring"])
        track = self._track_at(track_index)
        if self._is_frozen(track):
            raise ValueError("Track is frozen; monitoring was not changed")
        current = self._require_monitoring(track)
        changed = current != state
        if changed:
            track.current_monitoring_state = state
        readback = self._read_monitoring(track)
        return {
            "track_index": track_index,
            "monitoring": _MONITORING_NAME_BY_STATE.get(readback, "unknown"),
            "monitoring_value": readback,
            "changed": changed,
        }

    def _cmd_set_track_input_routing(self, params):
        self._reject_unknown(
            params, ("track_index", "input_routing_type", "input_routing_channel")
        )
        track_index = self._require_present_int(params, "track_index")
        type_name = params.get("input_routing_type")
        channel_name = params.get("input_routing_channel")
        if type_name is None and channel_name is None:
            raise ValueError(
                "input_routing_type or input_routing_channel is required"
            )
        track = self._track_at(track_index)
        if self._is_frozen(track):
            raise ValueError("Track is frozen; input routing was not changed")
        routing_type = None
        routing_channel = None
        deferred_channel = None
        if type_name is not None:
            options = self._routing_options(
                track, "available_input_routing_types", "input routing types"
            )
            routing_type = self._match_routing(options, type_name, "input routing type")
            if channel_name is not None:
                # Channel list changes with the type, so resolve it after the
                # type write. _assign_input_routing restores the type on failure.
                deferred_channel = channel_name
        elif channel_name is not None:
            options = self._routing_options(
                track, "available_input_routing_channels", "input routing channels"
            )
            routing_channel = self._match_routing(
                options, channel_name, "input routing channel"
            )
        self._assign_input_routing(track, routing_type, routing_channel, deferred_channel)
        return self._mixer_state(track_index, track)

    def _enabled_alias(self, params):
        return self._require_bool(
            self._pick_alias(params, ("enabled", "on"), True, "enabled"), "enabled"
        )

    def _cmd_set_arrangement_overdub(self, params):
        self._reject_unknown(params, ("enabled", "on"))
        enabled = self._enabled_alias(params)
        value, changed = self._assign_song_bool("arrangement_overdub", enabled)
        return {
            "arrangement_overdub": value,
            "changed": changed,
            "record_mode": self._read_song_bool("record_mode"),
            "note": (
                "This sets MIDI arrangement overdub only. It does not start "
                "or stop the transport or the arrangement record button."
            ),
        }

    def _cmd_set_arrangement_record(self, params):
        self._reject_unknown(params, ("enabled", "on", "allow_overwrite"))
        enabled = self._enabled_alias(params)
        allow = self._require_bool(params.get("allow_overwrite", False), "allow_overwrite")
        current = self._read_song_bool("record_mode")
        if current == enabled:
            return {
                "record_mode": current,
                "changed": False,
                "allow_overwrite": allow,
                "deleted": False,
            }
        blockers = []
        unverified = []
        song_time = None
        if enabled:
            song_time, blockers, unverified = self._arrangement_record_blockers()
            if not allow and (blockers or unverified or song_time is None):
                parts = []
                for item in blockers:
                    parts.append(
                        "track %s arrangement clip %s (%s to %s beats)"
                        % (
                            item.get("track_index"),
                            item.get("clip_index"),
                            item.get("start_time"),
                            item.get("end_time"),
                        )
                    )
                if unverified:
                    parts.append(
                        "could not verify arrangement clips on tracks %s"
                        % ", ".join(str(i) for i in unverified)
                    )
                raise ValueError(
                    "Refusing to enable arrangement record because it would "
                    "write over existing arrangement clips at the playhead, "
                    "or those clips could not be checked (%s). Pass "
                    "allow_overwrite=true to set the record button anyway. "
                    "record_mode was not changed."
                    % ("; ".join(parts) or "unverified")
                )
        value, changed = self._assign_song_bool("record_mode", enabled)
        result = self._song_transport_snapshot()
        result.update({
            "record_mode": value,
            "changed": changed,
            "allow_overwrite": allow,
            "existing_clips": blockers,
            "deleted": False,
            "note": (
                "record_mode is the arrangement record button only. Playback "
                "was not started or stopped. Recorded arrangement material is "
                "left in place when recording is turned off."
            ),
        })
        return result

    def _cmd_set_session_record(self, params):
        self._reject_unknown(params, ("enabled", "on", "allow_overwrite"))
        enabled = self._enabled_alias(params)
        allow = self._require_bool(params.get("allow_overwrite", False), "allow_overwrite")
        current = self._read_song_bool("session_record")
        if current == enabled:
            return {
                "session_record": current,
                "changed": False,
                "allow_overwrite": allow,
                "deleted": False,
            }
        scene_index = None
        armed = []
        conflicts = []
        if enabled:
            scene_index, armed, conflicts = self._session_record_blockers()
            unresolved = scene_index is None and len(armed) > 0
            if not allow and (conflicts or unresolved):
                if unresolved:
                    why = (
                        "the selected scene could not be read while tracks "
                        "%s are armed" % ", ".join(str(i) for i in armed)
                    )
                else:
                    why = self._format_slot_conflicts(conflicts)
                raise ValueError(
                    "Refusing to enable session record because it records "
                    "into the selected scene of every armed track and "
                    "overdubs a playing clip (%s). Pass allow_overwrite=true "
                    "to set the button anyway. session_record was not changed."
                    % why
                )
        value, changed = self._assign_song_bool("session_record", enabled)
        result = self._song_transport_snapshot()
        result.update({
            "session_record": value,
            "changed": changed,
            "allow_overwrite": allow,
            "existing_clips": conflicts,
            "armed_tracks": armed,
            "deleted": False,
            "note": (
                "session_record is the session record button. This does not "
                "call trigger_session_record, does not delete clips, and does "
                "not stop the transport. Turning it off leaves recorded clips "
                "in place. For one empty slot, use record_empty_slot."
            ),
        })
        return result

    def _cmd_record_empty_slot(self, params):
        self._reject_unknown(params, (
            "track_index", "clip_index", "arm", "armed", "record_length",
        ))
        track_index = self._require_present_int(params, "track_index")
        clip_index = self._require_present_int(params, "clip_index")
        if "arm" in params or "armed" in params:
            arm = self._require_bool(
                self._pick_alias(params, ("arm", "armed"), True, "arm"), "arm"
            )
        else:
            arm = True
        record_length = None
        if "record_length" in params and params["record_length"] is not None:
            record_length = self._require_record_length(params["record_length"])
        track = self._track_at(track_index)
        if self._is_frozen(track):
            raise ValueError("Track is frozen; nothing was armed or fired")
        if self._is_group_track(track):
            raise ValueError(
                "Group tracks are not recorded with record_empty_slot; "
                "nothing was armed or fired"
            )
        self._assert_can_arm(track)
        slot = self._slot_at(track, clip_index)
        if bool(getattr(slot, "has_clip", False)) or self._slot_is_recording(slot):
            raise ValueError(
                "Clip slot %d on track %d already has a clip or is recording. "
                "record_empty_slot will not overwrite it. Nothing was fired."
                % (clip_index, track_index)
            )
        if not callable(getattr(slot, "fire", None)):
            raise ValueError("Clip slot cannot be fired; nothing was changed")
        currently_armed = self._safe_arm(track)
        if not arm and not currently_armed:
            raise ValueError(
                "Track is not armed. Firing an empty slot on an unarmed track "
                "triggers that track's stop button, so the slot was not fired. "
                "Pass arm=true or arm the track first."
            )
        arm_changed = False
        if arm and not currently_armed:
            track.arm = True
            arm_changed = True
            if not self._safe_arm(track):
                raise ValueError("Track did not arm; the slot was not fired")
        try:
            self._fire_slot_exact(track, slot, record_length)
        except Exception:
            if arm_changed:
                try:
                    track.arm = False
                except Exception as restore_error:
                    self.log_message("arm restore failed: " + str(restore_error))
            raise
        warnings = list(self._input_warnings(track, track_index))
        transport_warnings, transport = self._transport_probe()
        warnings.extend(transport_warnings)
        clip = slot.clip if bool(getattr(slot, "has_clip", False)) else None
        result = {
            "track_index": track_index,
            "clip_index": clip_index,
            "triggered": True,
            "record_length": record_length,
            "arm": self._safe_arm(track),
            "arm_changed": arm_changed,
            "deleted": False,
            "has_clip": bool(getattr(slot, "has_clip", False)),
            "session_record": self._read_song_bool("session_record"),
            "exclusive_arm": self._optional_song_value("exclusive_arm", bool, None),
            "warnings": warnings,
            "note": (
                "The empty slot was fired. Live may wait for the current "
                "launch quantization before capturing. This response means "
                "the slot was fired, not that audio has been recorded, and "
                "it is not a real-time acknowledgement. Fixed length uses "
                "ClipSlot.fire(record_length) on this slot only. warnings "
                "do not block the fire."
            ),
        }
        result.update(transport)
        result.update(self._length_report(
            clip, recording=self._slot_is_recording(slot) or record_length is not None
        ))
        return result

    def _cmd_stop_recording(self, params):
        self._reject_unknown(params, ("scope", "track_index", "clip_index"))
        if "scope" not in params:
            raise ValueError("scope is required")
        scope = params["scope"]
        if not isinstance(scope, str) or scope not in _STOP_RECORDING_SCOPES:
            raise ValueError(
                "scope must be one of: arrangement, session, slot, recording"
            )
        if scope != "slot" and (
            "track_index" in params or "clip_index" in params
        ):
            raise ValueError(
                "track_index and clip_index are only valid when scope is slot"
            )
        if scope == "arrangement":
            value, changed = self._assign_song_bool("record_mode", False)
            result = self._song_transport_snapshot()
            result.update({
                "scope": "arrangement",
                "changed": changed,
                "record_mode": value,
                "deleted": False,
                "note": (
                    "Arrangement record was set off. Playback was not stopped "
                    "and clips were not deleted."
                ),
            })
            return result
        if scope == "session":
            value, changed = self._assign_song_bool("session_record", False)
            result = self._song_transport_snapshot()
            result.update({
                "scope": "session",
                "changed": changed,
                "session_record": value,
                "deleted": False,
                "note": (
                    "Session record was set off. Playback was not stopped "
                    "and clips were not deleted."
                ),
            })
            return result
        if scope == "slot":
            return self._stop_recording_slot(params)
        return self._stop_all_recording_activity()

    def _stop_recording_slot(self, params):
        track_index = self._require_present_int(params, "track_index")
        clip_index = self._require_present_int(params, "clip_index")
        track = self._track_at(track_index)
        slot = self._slot_at(track, clip_index)
        if not self._slot_is_recording(slot):
            return {
                "scope": "slot",
                "track_index": track_index,
                "clip_index": clip_index,
                "changed": False,
                "stopped": False,
                "deleted": False,
                "note": "Slot is not recording; stop was not called.",
            }
        if self._is_group_track(track):
            raise ValueError(
                "Refusing to stop a group-track slot; Live would stop every "
                "track in the group. Stop the child track that is recording."
            )
        slot.stop()
        return {
            "scope": "slot",
            "track_index": track_index,
            "clip_index": clip_index,
            "changed": True,
            "stopped": True,
            "deleted": False,
            "record_mode": self._read_song_bool("record_mode"),
            "session_record": self._read_song_bool("session_record"),
            "is_playing": self._optional_song_value("is_playing", bool, None),
            "note": (
                "ClipSlot.stop() ends session clips on this track. The "
                "recorded clip is kept. Other tracks, arrangement record, "
                "and the transport were not changed."
            ),
        }

    def _stop_all_recording_activity(self):
        changed = False
        if self._read_song_bool("record_mode"):
            self._assign_song_bool("record_mode", False)
            changed = True
        if self._read_song_bool("session_record"):
            self._assign_song_bool("session_record", False)
            changed = True
        stopped_tracks = []
        for track_index, track in enumerate(self._iter_tracks()):
            if self._is_group_track(track):
                continue
            slots = self._recording_slot_indexes(track)
            if not slots:
                continue
            self._slot_at(track, slots[0]).stop()
            stopped_tracks.append({
                "track_index": track_index,
                "clip_indexes": slots,
            })
            changed = True
        result = self._song_transport_snapshot()
        result.update({
            "scope": "recording",
            "changed": changed,
            "stopped_tracks": stopped_tracks,
            "deleted": False,
            "note": (
                "Arrangement record and session record were set off, and "
                "session clips were stopped only on tracks that were "
                "recording. Playing tracks were left playing. Nothing was "
                "deleted and the transport was not stopped."
            ),
        })
        return result

    def _normalize_sends(self, raw):
        if not isinstance(raw, list):
            raise ValueError("sends must be a list of {index, value} objects")
        normalized = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("each send must be an object with index and value")
            extra = [key for key in item.keys() if key not in ("index", "value")]
            if extra:
                raise ValueError("send objects only accept index and value")
            if "index" not in item or "value" not in item:
                raise ValueError("each send needs index and value")
            index = self._require_int(item["index"], "send index")
            if index in seen:
                raise ValueError("duplicate send index %d" % index)
            seen.add(index)
            normalized.append((index, item["value"]))
        return normalized

    def _plan_prepare(self, track, params):
        """Validate a prepare_track request. Does not write."""
        plan = {
            "name": None,
            "arm": None,
            "mute": None,
            "solo": None,
            "volume": None,
            "panning": None,
            "sends": [],
            "monitoring": None,
            "routing_type": None,
            "routing_channel": None,
            "routing_channel_name": None,
        }
        if "name" in params:
            name = params["name"]
            if not isinstance(name, str):
                raise ValueError("name must be a string")
            if len(name) > 256:
                raise ValueError("name must be at most 256 characters")
            plan["name"] = name
        for attr, key in (("arm", "arm"), ("mute", "mute"), ("solo", "solo")):
            if key in params:
                plan[attr] = self._require_bool(params[key], key)
        if plan["arm"] is not None:
            self._assert_can_arm(track)
        mixer = None
        if "volume" in params or "panning" in params or "sends" in params:
            mixer = self._mixer(track)
        if "volume" in params:
            # _assign_param validates and would write. Validate range only.
            self._check_param_range(mixer.volume, params["volume"], "volume")
            plan["volume"] = self._finite_float(params["volume"], "volume")
        if "panning" in params:
            self._check_param_range(mixer.panning, params["panning"], "panning")
            plan["panning"] = self._finite_float(params["panning"], "panning")
        if "sends" in params:
            sends = list(mixer.sends)
            for index, value in self._normalize_sends(params["sends"]):
                if index < 0 or index >= len(sends):
                    raise ValueError(
                        "send_index out of range (track has %d sends)" % len(sends)
                    )
                self._check_param_range(sends[index], value, "send")
                plan["sends"].append((index, self._finite_float(value, "send")))
        if "monitoring" in params:
            if self._is_frozen(track):
                raise ValueError("Track is frozen; monitoring was not changed")
            self._require_monitoring(track)
            plan["monitoring"] = self._monitoring_value(params["monitoring"])
        type_name = params.get("input_routing_type")
        channel_name = params.get("input_routing_channel")
        if type_name is not None or channel_name is not None:
            if self._is_frozen(track):
                raise ValueError("Track is frozen; input routing was not changed")
            if type_name is not None:
                options = self._routing_options(
                    track, "available_input_routing_types", "input routing types"
                )
                plan["routing_type"] = self._match_routing(
                    options, type_name, "input routing type"
                )
                if channel_name is not None:
                    plan["routing_channel_name"] = channel_name
                    if not isinstance(channel_name, str) or channel_name.strip() == "":
                        raise ValueError(
                            "input routing channel must be a non-empty string"
                        )
            elif channel_name is not None:
                options = self._routing_options(
                    track,
                    "available_input_routing_channels",
                    "input routing channels",
                )
                plan["routing_channel"] = self._match_routing(
                    options, channel_name, "input routing channel"
                )
        writes = any([
            plan["name"] is not None,
            plan["arm"] is not None,
            plan["mute"] is not None,
            plan["solo"] is not None,
            plan["volume"] is not None,
            plan["panning"] is not None,
            plan["sends"],
            plan["monitoring"] is not None,
            plan["routing_type"] is not None,
            plan["routing_channel"] is not None,
            plan["routing_channel_name"] is not None,
        ])
        plan["writes"] = writes
        return plan

    def _check_param_range(self, param, value, label):
        value = self._finite_float(value, label)
        try:
            lo = float(param.min)
            hi = float(param.max)
        except Exception as e:
            raise ValueError("%s range could not be read (%s)" % (label, e))
        if value < lo or value > hi:
            raise ValueError("%s must be between %s and %s" % (label, lo, hi))
        return value

    def _apply_prepare(self, track, plan):
        """Apply a validated plan. Returns True when a Live value changed."""
        changed = False
        # Routing first so a failed channel match restores the type before
        # any other field is written.
        if (
            plan["routing_type"] is not None
            or plan["routing_channel"] is not None
            or plan["routing_channel_name"] is not None
        ):
            changed = self._assign_input_routing(
                track,
                plan["routing_type"],
                plan["routing_channel"],
                plan["routing_channel_name"],
            ) or changed
        if plan["name"] is not None and track.name != plan["name"]:
            track.name = plan["name"]
            changed = True
        if plan["monitoring"] is not None:
            current = self._read_monitoring(track)
            if current != plan["monitoring"]:
                track.current_monitoring_state = plan["monitoring"]
                changed = True
        mixer = None
        if plan["volume"] is not None or plan["panning"] is not None or plan["sends"]:
            mixer = self._mixer(track)
        if plan["volume"] is not None:
            _value, wrote = self._assign_param(mixer.volume, plan["volume"], "volume")
            changed = changed or wrote
        if plan["panning"] is not None:
            _value, wrote = self._assign_param(mixer.panning, plan["panning"], "panning")
            changed = changed or wrote
        if plan["sends"]:
            sends = list(mixer.sends)
            for index, value in plan["sends"]:
                _value, wrote = self._assign_param(sends[index], value, "send")
                changed = changed or wrote
        if plan["mute"] is not None:
            _value, wrote = self._assign_bool_attr(track, "mute", plan["mute"])
            changed = changed or wrote
        if plan["solo"] is not None:
            _value, wrote = self._assign_bool_attr(track, "solo", plan["solo"])
            changed = changed or wrote
        if plan["arm"] is not None:
            _value, wrote = self._assign_bool_attr(track, "arm", plan["arm"])
            changed = changed or wrote
        return changed

    def _fold_named_aliases(self, params, groups):
        folded = dict(params)
        for canonical, names in groups:
            if not any(name in params for name in names):
                continue
            folded[canonical] = self._pick_alias(params, names, True, canonical)
            for name in names:
                if name != canonical:
                    folded.pop(name, None)
        return folded

    def _cmd_prepare_track(self, params):
        self._reject_unknown(params, (
            "track_index",
            "name",
            "arm",
            "armed",
            "mute",
            "solo",
            "volume",
            "value",
            "panning",
            "pan",
            "sends",
            "monitoring",
            "input_routing_type",
            "input_routing_channel",
        ))
        params = self._fold_named_aliases(params, (
            ("volume", ("volume", "value")),
            ("panning", ("panning", "pan")),
            ("arm", ("arm", "armed")),
        ))
        if params.get("monitoring") == "keep":
            params = dict(params)
            params.pop("monitoring")
        elif params.get("arm") is True and "monitoring" not in params:
            params = dict(params)
            params["monitoring"] = "auto"
        track_index = self._require_present_int(params, "track_index")
        track = self._track_at(track_index)
        plan = self._plan_prepare(track, params)
        changed = False
        if plan["writes"]:
            changed = self._apply_prepare(track, plan)
        state = self._mixer_state(track_index, track)
        state["changed"] = bool(changed)
        state["note"] = (
            "prepare_track applies only the fields you send, in one main-thread "
            "turn. Omitted fields are left alone. This does not start recording."
        )
        return state

    def _clip_for_region(self, track, clip_index, view):
        if view == "session":
            slot = self._slot_at(track, clip_index)
            if not bool(getattr(slot, "has_clip", False)):
                raise ValueError("No clip in slot")
            return slot.clip
        try:
            clips = list(track.arrangement_clips)
        except Exception as e:
            raise ValueError("Arrangement clips are not available (%s)" % e)
        if clip_index < 0 or clip_index >= len(clips):
            raise IndexError("Arrangement clip index out of range")
        return clips[clip_index]

    def _region_units(self, clip):
        is_audio = bool(getattr(clip, "is_audio_clip", False))
        try:
            warping = getattr(clip, "warping")
        except Exception:
            warping = None
        if not is_audio or warping:
            return "beats", warping
        if warping is False:
            return "seconds", warping
        return "unknown", warping

    def _clip_region_payload(self, track_index, clip_index, view, clip):
        units, warping = self._region_units(clip)

        def _num(attr):
            try:
                value = getattr(clip, attr)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return None
                value = float(value)
                if value != value or value in (float("inf"), float("-inf")):
                    return None
                return value
            except Exception:
                return None

        return {
            "track_index": track_index,
            "clip_index": clip_index,
            "view": view,
            "name": getattr(clip, "name", ""),
            "is_audio_clip": bool(getattr(clip, "is_audio_clip", False)),
            "is_midi_clip": bool(getattr(clip, "is_midi_clip", False)),
            "is_recording": bool(getattr(clip, "is_recording", False)),
            "warping": None if warping is None else bool(warping),
            "units": units,
            "length": self._length_report(clip).get("length"),
            "length_status": self._length_report(clip).get("length_status"),
            "start_marker": _num("start_marker"),
            "end_marker": _num("end_marker"),
            "loop_start": _num("loop_start"),
            "loop_end": _num("loop_end"),
            "looping": self._optional_bool_attr(clip, "looping"),
            "crop": False,
            "note": (
                "Markers describe the playback region. They do not crop the "
                "clip or replace the source file. Units are beats for MIDI "
                "and warped audio, and seconds for unwarped audio."
            ),
        }

    def _optional_bool_attr(self, obj, attr):
        try:
            value = getattr(obj, attr)
        except Exception:
            return None
        if isinstance(value, bool):
            return value
        return None

    def _cmd_get_clip_region(self, params):
        self._reject_unknown(params, ("track_index", "clip_index", "view"))
        track_index = self._require_present_int(params, "track_index")
        clip_index = self._require_present_int(params, "clip_index")
        view = params.get("view", "session")
        if not isinstance(view, str) or view not in _CLIP_REGION_VIEWS:
            raise ValueError("view must be session or arrangement")
        track = self._track_at(track_index)
        clip = self._clip_for_region(track, clip_index, view)
        return self._clip_region_payload(track_index, clip_index, view, clip)

    def _read_marker(self, clip, attr):
        try:
            value = getattr(clip, attr)
        except Exception as e:
            raise ValueError("Could not read %s (%s); no markers were changed" % (attr, e))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Could not read %s; no markers were changed" % attr)
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("Could not read %s; no markers were changed" % attr)
        return value

    def _restore_clip_region(self, clip, saved):
        start = saved["start"]
        end = saved["end"]
        loop_start = saved.get("loop_start")
        loop_end = saved.get("loop_end")
        looping = saved.get("looping")
        try:
            if float(clip.end_marker) < float(end):
                clip.end_marker = end
            if float(clip.start_marker) != float(start):
                clip.start_marker = start
            if float(clip.end_marker) != float(end):
                clip.end_marker = end
            if loop_start is not None and loop_end is not None:
                if float(clip.loop_end) < float(loop_end):
                    clip.loop_end = loop_end
                if float(clip.loop_start) != float(loop_start):
                    clip.loop_start = loop_start
                if float(clip.loop_end) != float(loop_end):
                    clip.loop_end = loop_end
            if looping is not None and bool(clip.looping) != bool(looping):
                clip.looping = looping
        except Exception as restore_error:
            self.log_message("clip region restore failed: " + str(restore_error))

    def _cmd_set_clip_region(self, params):
        self._reject_unknown(params, (
            "track_index",
            "clip_index",
            "view",
            "start_marker",
            "end_marker",
            "loop_start",
            "loop_end",
            "looping",
        ))
        track_index = self._require_present_int(params, "track_index")
        clip_index = self._require_present_int(params, "clip_index")
        view = params.get("view", "session")
        if not isinstance(view, str) or view not in _CLIP_REGION_VIEWS:
            raise ValueError("view must be session or arrangement")
        fields = ("start_marker", "end_marker", "loop_start", "loop_end", "looping")
        if not any(name in params for name in fields):
            raise ValueError(
                "Set at least one of start_marker, end_marker, loop_start, "
                "loop_end, looping"
            )
        track = self._track_at(track_index)
        clip = self._clip_for_region(track, clip_index, view)
        if bool(getattr(clip, "is_recording", False)):
            raise ValueError(
                "Clip is recording; markers were not changed. Stop recording first."
            )
        cur_start = self._read_marker(clip, "start_marker")
        cur_end = self._read_marker(clip, "end_marker")
        new_start = cur_start
        new_end = cur_end
        if "start_marker" in params:
            new_start = self._finite_float(params["start_marker"], "start_marker")
        if "end_marker" in params:
            new_end = self._finite_float(params["end_marker"], "end_marker")
        if new_end <= new_start:
            raise ValueError(
                "end_marker must be greater than start_marker; no markers were changed"
            )
        touch_loop = any(name in params for name in ("loop_start", "loop_end", "looping"))
        cur_loop_start = cur_loop_end = None
        new_loop_start = new_loop_end = None
        new_looping = None
        if touch_loop or "start_marker" in params or "end_marker" in params:
            try:
                cur_loop_start = self._read_marker(clip, "loop_start")
                cur_loop_end = self._read_marker(clip, "loop_end")
            except ValueError:
                if touch_loop:
                    raise
                cur_loop_start = None
                cur_loop_end = None
        explicit_loop = "loop_start" in params or "loop_end" in params
        loop_adjusted = False
        if cur_loop_start is not None:
            new_loop_start = cur_loop_start
            new_loop_end = cur_loop_end
            if "loop_start" in params:
                new_loop_start = self._finite_float(params["loop_start"], "loop_start")
            if "loop_end" in params:
                new_loop_end = self._finite_float(params["loop_end"], "loop_end")
            if explicit_loop:
                if new_loop_end <= new_loop_start:
                    raise ValueError(
                        "loop_end must be greater than loop_start; no markers were changed"
                    )
                if new_loop_start < new_start - 1e-9 or new_loop_end > new_end + 1e-9:
                    raise ValueError(
                        "loop range must lie within start_marker and end_marker; "
                        "no markers were changed"
                    )
            else:
                # Markers alone: pull the existing loop inside the new region
                # instead of refusing. A loop that falls entirely outside
                # becomes the new marker span so a looping clip stays loopable.
                clamped_start = new_loop_start
                clamped_end = new_loop_end
                if clamped_start < new_start:
                    clamped_start = new_start
                if clamped_end > new_end:
                    clamped_end = new_end
                if clamped_end <= clamped_start + 1e-9:
                    clamped_start = new_start
                    clamped_end = new_end
                if (
                    abs(clamped_start - new_loop_start) > 1e-9
                    or abs(clamped_end - new_loop_end) > 1e-9
                ):
                    loop_adjusted = True
                new_loop_start = clamped_start
                new_loop_end = clamped_end
        if "looping" in params:
            new_looping = self._require_bool(params["looping"], "looping")
            is_audio = bool(getattr(clip, "is_audio_clip", False))
            warping = getattr(clip, "warping", None)
            if new_looping is True and is_audio and warping is False:
                raise ValueError(
                    "Unwarped audio clips cannot be looped; no markers were changed"
                )
        looping_now = None
        try:
            if isinstance(getattr(clip, "looping"), bool):
                looping_now = bool(clip.looping)
        except Exception:
            looping_now = None
        saved = {
            "start": cur_start,
            "end": cur_end,
            "loop_start": cur_loop_start,
            "loop_end": cur_loop_end,
            "looping": looping_now,
        }
        marker_changes = new_start != cur_start or new_end != cur_end
        loop_changes = (
            cur_loop_start is not None
            and (new_loop_start != cur_loop_start or new_loop_end != cur_loop_end)
        )
        looping_changes = new_looping is not None and new_looping != looping_now
        if not marker_changes and not loop_changes and not looping_changes:
            payload = self._clip_region_payload(track_index, clip_index, view, clip)
            payload["changed"] = False
            payload["deleted"] = False
            return payload
        try:
            # Widen markers, move the loop inside that wider region, then
            # shrink markers. That order keeps start <= end and the loop
            # inside the markers at every step.
            if new_end > cur_end:
                clip.end_marker = new_end
            if new_start < cur_start:
                clip.start_marker = new_start
            if loop_changes:
                if new_loop_end > cur_loop_end:
                    clip.loop_end = new_loop_end
                if new_loop_start != cur_loop_start:
                    clip.loop_start = new_loop_start
                if new_loop_end != cur_loop_end and new_loop_end <= cur_loop_end:
                    clip.loop_end = new_loop_end
            if new_start > cur_start:
                clip.start_marker = new_start
            if new_end < cur_end:
                clip.end_marker = new_end
            if looping_changes:
                clip.looping = new_looping
        except Exception:
            self._restore_clip_region(clip, saved)
            raise
        payload = self._clip_region_payload(track_index, clip_index, view, clip)
        payload["changed"] = True
        payload["deleted"] = False
        payload["loop_adjusted"] = loop_adjusted
        payload["crop"] = False
        return payload

    def _optional_flag(self, params, name, default):
        if name not in params or params[name] is None:
            return default
        return self._require_bool(params[name], name)

    def _optional_pre_roll(self, params):
        if "pre_roll_bars" not in params or params["pre_roll_bars"] is None:
            return 0.0
        bars = self._finite_float(params["pre_roll_bars"], "pre_roll_bars")
        if bars < 0:
            raise ValueError("pre_roll_bars must be >= 0")
        return bars

    def _fire_slot_exact(self, track, slot, record_length):
        """Fire an empty slot.

        A fixed length uses launch quantization None for this call only, after
        stopping any other playing or recording slot on the track. Live's
        fire(record_length) relaunches the clip at the end, and a global
        quantize (or the previous take's relaunch) was cutting takes to 8 or
        12 beats instead of the requested 16. The song quantization setting
        is not modified.
        """
        if record_length is None:
            slot.fire()
            return False
        self._stop_other_slot_playback(track, slot)
        slot.fire(float(record_length), _LAUNCH_QUANTIZATION_NONE)
        return True

    def _stop_other_slot_playback(self, track, target_slot):
        try:
            slots = list(track.clip_slots)
        except Exception:
            return False
        for slot in slots:
            if slot is target_slot:
                continue
            active = bool(getattr(slot, "is_playing", False)) or self._slot_is_recording(slot)
            if active and callable(getattr(slot, "stop", None)):
                slot.stop()
                return True
        return False

    def _apply_recording_monitoring(self, track, monitoring):
        """Default Auto when a recording command arms. 'keep' leaves it."""
        if monitoring == "keep":
            return False
        if monitoring is None:
            monitoring = "auto"
        self._require_monitoring(track)
        value = self._monitoring_value(monitoring)
        if self._read_monitoring(track) != value:
            track.current_monitoring_state = value
            return True
        return False

    def _bars_to_beats(self, bars):
        bars = self._finite_float(bars, "bars")
        if bars <= 0:
            raise ValueError("bars must be positive")
        numer = self._optional_song_value("signature_numerator", int, 4)
        if isinstance(numer, bool) or not isinstance(numer, int) or numer < 1:
            numer = 4
        return self._require_record_length(bars * float(numer)), numer

    def _begin_pre_roll(self, bars):
        try:
            self._song.metronome = True
        except Exception as e:
            self.log_message("metronome for pre-roll failed: " + str(e))
        try:
            if not bool(self._song.is_playing):
                self._song.start_playing()
        except Exception as e:
            self.log_message("start_playing for pre-roll failed: " + str(e))
        beats, _numer = self._bars_to_beats(bars)
        try:
            start = float(self._song.current_song_time)
        except Exception:
            start = 0.0
        return start + beats

    def _schedule_pre_roll_fire(self, track, track_index, slot, slot_index, beats, arm, disarm_after, pre_roll):
        target = self._begin_pre_roll(pre_roll)
        state = {"tries": 0, "target": target, "done": False}

        def poll():
            if state["done"]:
                return
            state["tries"] += 1
            if state["tries"] > 400:
                self.log_message("pre-roll gave up; transport did not reach the record point")
                return
            try:
                now = float(self._song.current_song_time)
            except Exception:
                now = 0.0
            if now + 1e-4 < state["target"]:
                self._schedule_later(1, poll)
                return
            state["done"] = True
            try:
                self._fire_empty_slot(track, track_index, slot, slot_index, beats, arm)
                if disarm_after:
                    self._schedule_disarm_when_take_ends(track, slot)
            except Exception as e:
                self.log_message("pre-roll fire failed: " + str(e))

        self._schedule_later(1, poll)

    def _quick_record_result(self, track, track_index, slot, slot_index, beats, arm, arm_changed, disarm_after, triggered, pre_roll):
        warnings = list(self._input_warnings(track, track_index))
        transport_warnings, transport = self._transport_probe()
        warnings.extend(transport_warnings)
        clip = slot.clip if bool(getattr(slot, "has_clip", False)) else None
        result = {
            "track_index": track_index,
            "clip_index": slot_index,
            "triggered": triggered,
            "record_length": beats,
            "launch_quantization": "none_for_this_fire" if triggered else None,
            "song_quantization_changed": False,
            "arm": self._safe_arm(track),
            "arm_changed": arm_changed,
            "disarm_after": "when_take_ends" if disarm_after else False,
            "has_clip": bool(getattr(slot, "has_clip", False)),
            "deleted": False,
            "quantization_changed": False,
            "pre_roll_bars": pre_roll or None,
            "warnings": warnings,
            "note": (
                "Fixed-length fire uses launch quantization none for that "
                "call only, after stopping any other clip on the track, so "
                "record_length is not shortened by the global grid or by the "
                "previous take relaunching. The song quantization setting is "
                "not changed. disarm_after waits until the take stops "
                "recording. This reply is not a real-time acknowledgement."
            ),
        }
        result.update(transport)
        result.update(self._length_report(clip, recording=triggered))
        return result

    def _append_scene_and_slot(self, track):
        create = getattr(self._song, "create_scene", None)
        if not callable(create):
            raise ValueError("This Live song cannot create a scene; no slot was fired")
        create(-1)
        try:
            return len(list(track.clip_slots)) - 1
        except Exception:
            scenes = list(self._song.scenes)
            return len(scenes) - 1

    def _schedule_disarm_when_take_ends(self, track, slot):
        state = {"tries": 0, "clip": None, "listener": None, "done": False}

        def finished():
            if state["done"]:
                return
            state["done"] = True
            clip = state["clip"]
            listener = state["listener"]
            if clip is not None and listener is not None:
                try:
                    clip.remove_is_recording_listener(listener)
                except Exception:
                    pass
            try:
                if self._safe_arm(track):
                    track.arm = False
            except Exception as e:
                self.log_message("disarm_after failed: " + str(e))

        def on_change():
            try:
                if not bool(state["clip"].is_recording):
                    finished()
            except Exception:
                finished()

        def poll():
            if state["done"] or state["listener"] is not None:
                return
            state["tries"] += 1
            if state["tries"] > 240:
                return
            try:
                if bool(getattr(slot, "has_clip", False)):
                    clip = slot.clip
                    state["clip"] = clip
                    recording = bool(getattr(clip, "is_recording", False))
                    if recording and callable(getattr(clip, "add_is_recording_listener", None)):
                        clip.add_is_recording_listener(on_change)
                        state["listener"] = on_change
                        return
                    if not recording and state["tries"] > 2:
                        finished()
                        return
            except Exception:
                pass
            self._schedule_later(1, poll)

        self._schedule_later(1, poll)

    def _discard_recording_on_track(self, track):
        """Stop and delete only slots that are recording. Finished takes stay."""
        indexes = list(self._recording_slot_indexes(track))
        discarded = []
        if not indexes:
            return discarded
        first = self._slot_at(track, indexes[0])
        if callable(getattr(first, "stop", None)):
            try:
                first.stop()
            except Exception as e:
                self.log_message("stop before discard failed: " + str(e))
        for index in indexes:
            slot = self._slot_at(track, index)
            name = ""
            try:
                if bool(getattr(slot, "has_clip", False)):
                    name = str(slot.clip.name)
            except Exception:
                name = ""
            deleted = False
            if bool(getattr(slot, "has_clip", False)) and callable(getattr(slot, "delete_clip", None)):
                try:
                    slot.delete_clip()
                    deleted = not bool(getattr(slot, "has_clip", True))
                    if deleted is False:
                        deleted = True
                except Exception as e:
                    self.log_message("discard delete failed: " + str(e))
                    deleted = False
            discarded.append({
                "clip_index": index,
                "name": name,
                "deleted": deleted,
            })
        return discarded

    def _record_loops(self):
        loops = getattr(self, "_record_loop_state", None)
        if loops is None:
            loops = {}
            self._record_loop_state = loops
        return loops

    def _first_empty_slot_index(self, track):
        try:
            slots = list(track.clip_slots)
        except Exception:
            return None
        for index, slot in enumerate(slots):
            if not bool(getattr(slot, "has_clip", False)) and not self._slot_is_recording(slot):
                return index
        return None

    def _beats_from_bars_or_beats(self, params):
        has_bars = "bars" in params and params["bars"] is not None
        has_beats = "beats" in params and params["beats"] is not None
        if has_bars == has_beats:
            raise ValueError("Pass bars or beats, not both and not neither")
        if has_beats:
            return self._require_record_length(params["beats"])
        bars = self._finite_float(params["bars"], "bars")
        if bars <= 0:
            raise ValueError("bars must be positive")
        numer = self._optional_song_value("signature_numerator", int, 4)
        if isinstance(numer, bool) or not isinstance(numer, int) or numer < 1:
            numer = 4
        return self._require_record_length(bars * float(numer))

    def _fire_empty_slot(self, track, track_index, slot, clip_index, record_length, arm):
        """Shared empty-slot fire used by record_empty_slot and quick_record."""
        currently_armed = self._safe_arm(track)
        if not arm and not currently_armed:
            raise ValueError(
                "Track is not armed. Firing an empty slot on an unarmed track "
                "triggers that track's stop button, so the slot was not fired."
            )
        arm_changed = False
        if arm and not currently_armed:
            track.arm = True
            arm_changed = True
            if not self._safe_arm(track):
                raise ValueError("Track did not arm; the slot was not fired")
        try:
            self._fire_slot_exact(track, slot, record_length)
        except Exception:
            if arm_changed:
                try:
                    track.arm = False
                except Exception as restore_error:
                    self.log_message("arm restore failed: " + str(restore_error))
            raise
        return arm_changed

    def _cmd_quick_record(self, params):
        self._reject_unknown(params, (
            "track_index", "bars", "beats", "slot_index", "clip_index",
            "arm", "armed", "monitoring", "input", "input_routing_type",
            "input_routing_channel", "disarm_after", "pre_roll_bars",
            "create_scene_if_full",
        ))
        track_index = self._require_present_int(params, "track_index")
        beats = self._beats_from_bars_or_beats(params)
        if "arm" in params or "armed" in params:
            arm = self._require_bool(
                self._pick_alias(params, ("arm", "armed"), True, "arm"), "arm"
            )
        else:
            arm = True
        disarm_after = False
        if "disarm_after" in params:
            disarm_after = self._require_bool(params["disarm_after"], "disarm_after")
        slot_index = None
        if "slot_index" in params or "clip_index" in params:
            slot_index = self._require_int(
                self._pick_alias(params, ("slot_index", "clip_index"), True, "slot_index"),
                "slot_index",
            )
        track = self._track_at(track_index)
        if self._is_frozen(track) or self._is_group_track(track):
            raise ValueError("Track cannot host quick_record; nothing was fired")
        self._assert_can_arm(track)
        if slot_index is None:
            slot_index = self._first_empty_slot_index(track)
            if slot_index is None:
                if self._optional_flag(params, "create_scene_if_full", False):
                    slot_index = self._append_scene_and_slot(track)
                else:
                    raise ValueError("No empty clip slot; nothing was fired")
        slot = self._slot_at(track, slot_index)
        if bool(getattr(slot, "has_clip", False)) or self._slot_is_recording(slot):
            raise ValueError(
                "Clip slot %d on track %d is not empty. Nothing was fired."
                % (slot_index, track_index)
            )
        if not callable(getattr(slot, "fire", None)):
            raise ValueError("Clip slot cannot be fired; nothing was changed")
        if (
            "input" in params and "input_routing_type" in params
            and params["input"] != params["input_routing_type"]
        ):
            raise ValueError("Conflicting input and input_routing_type")
        type_name = params.get("input_routing_type", params.get("input"))
        channel_name = params.get("input_routing_channel")
        routing_type = None
        routing_channel = None
        deferred_channel = None
        if type_name is not None:
            options = self._routing_options(
                track, "available_input_routing_types", "input routing types"
            )
            routing_type = self._match_routing(options, type_name, "input routing type")
            if channel_name is not None:
                if not isinstance(channel_name, str) or channel_name.strip() == "":
                    raise ValueError("input routing channel must be a non-empty string")
                deferred_channel = channel_name
        elif channel_name is not None:
            options = self._routing_options(
                track, "available_input_routing_channels", "input routing channels"
            )
            routing_channel = self._match_routing(
                options, channel_name, "input routing channel"
            )
        if routing_type is not None or routing_channel is not None or deferred_channel is not None:
            self._assign_input_routing(
                track, routing_type, routing_channel, deferred_channel
            )
        self._apply_recording_monitoring(track, params.get("monitoring", None))
        pre_roll = self._optional_pre_roll(params)
        if pre_roll:
            self._schedule_pre_roll_fire(
                track, track_index, slot, slot_index, beats, arm, disarm_after, pre_roll
            )
            return self._quick_record_result(
                track, track_index, slot, slot_index, beats, arm, False,
                disarm_after, triggered=False, pre_roll=pre_roll,
            )
        arm_changed = self._fire_empty_slot(
            track, track_index, slot, slot_index, beats, arm
        )
        if disarm_after:
            self._schedule_disarm_when_take_ends(track, slot)
        warnings = list(self._input_warnings(track, track_index))
        transport_warnings, transport = self._transport_probe()
        warnings.extend(transport_warnings)
        clip = slot.clip if bool(getattr(slot, "has_clip", False)) else None
        result = {
            "track_index": track_index,
            "clip_index": slot_index,
            "triggered": True,
            "record_length": beats,
            "arm": self._safe_arm(track),
            "arm_changed": arm_changed,
            "disarm_after": "when_take_ends" if disarm_after else False,
            "has_clip": bool(getattr(slot, "has_clip", False)),
            "deleted": False,
            "quantization_changed": False,
            "warnings": warnings,
            "note": (
                "Fixed-length fire uses launch quantization none for this "
                "call only. The song setting is not changed. disarm_after "
                "waits until recording ends. This is not real-time."
            ),
        }
        result.update(transport)
        result.update(self._length_report(clip, recording=True))
        return result

    def _arrangement_targets(self, indexes):
        targets = []
        for index in indexes:
            targets.append((index, self._track_at(index)))
        return targets

    def _cmd_quick_arrangement_record(self, params):
        self._reject_unknown(params, (
            "track_indexes", "start_time", "arm", "armed", "allow_overwrite",
        ))
        raw_indexes = params.get("track_indexes")
        if not isinstance(raw_indexes, list) or not raw_indexes:
            raise ValueError("track_indexes must be a non-empty list")
        indexes = [self._require_int(item, "track_indexes") for item in raw_indexes]
        if len(set(indexes)) != len(indexes):
            raise ValueError("duplicate track_indexes")
        if "arm" in params or "armed" in params:
            arm = self._require_bool(
                self._pick_alias(params, ("arm", "armed"), True, "arm"), "arm"
            )
        else:
            arm = True
        allow = self._require_bool(
            params.get("allow_overwrite", False), "allow_overwrite"
        )
        start_time = None
        if "start_time" in params and params["start_time"] is not None:
            start_time = self._finite_float(params["start_time"], "start_time")
            if start_time < 0:
                raise ValueError("start_time must be >= 0")
        targets = self._arrangement_targets(indexes)
        if arm:
            for _index, track in targets:
                self._assert_can_arm(track)
        check_time = start_time
        if check_time is None:
            check_time = self._optional_song_value("current_song_time", float, None)
        conflicts = []
        unverified = []
        if check_time is None:
            unverified.append("song_time")
        else:
            for index, track in targets:
                try:
                    clips = list(track.arrangement_clips)
                except Exception:
                    unverified.append(index)
                    continue
                for clip_index, clip in enumerate(clips):
                    try:
                        start = float(clip.start_time)
                        end = float(clip.end_time)
                    except Exception:
                        unverified.append(index)
                        break
                    if start <= float(check_time) < end:
                        conflicts.append({
                            "track_index": index,
                            "clip_index": clip_index,
                            "start_time": start,
                            "end_time": end,
                        })
        if not allow and (conflicts or unverified):
            raise ValueError(
                "Refusing quick_arrangement_record because it would write "
                "over arrangement clips or those clips could not be checked. "
                "Nothing was armed, sought, or recorded."
            )
        if not callable(getattr(self._song, "start_playing", None)):
            raise ValueError("Song has no start_playing; nothing was changed")
        armed_by_us = []
        try:
            if arm:
                for _index, track in targets:
                    if not self._safe_arm(track):
                        track.arm = True
                        armed_by_us.append(track)
            if start_time is not None:
                self._song.current_song_time = start_time
            self._assign_song_bool("record_mode", True)
            self._song.start_playing()
        except Exception:
            try:
                self._assign_song_bool("record_mode", False)
            except Exception:
                pass
            for track in armed_by_us:
                try:
                    track.arm = False
                except Exception:
                    pass
            raise
        return {
            "track_indexes": indexes,
            "record_mode": self._read_song_bool("record_mode"),
            "is_playing": self._optional_song_value("is_playing", bool, None),
            "current_song_time": self._optional_song_value(
                "current_song_time", float, None
            ),
            "armed": [self._safe_arm(track) for _index, track in targets],
            "deleted": False,
            "quantization_changed": False,
            "existing_clips": conflicts,
            "note": (
                "Armed the listed tracks, sought if start_time was sent, "
                "enabled arrangement record, and started playback in one "
                "call. Launch quantization was not changed."
            ),
        }

    def _cmd_quick_arrangement_stop(self, params):
        self._reject_unknown(params, (
            "track_indexes", "disarm", "disarmed", "discard_current",
        ))
        indexes = None
        if "track_indexes" in params and params["track_indexes"] is not None:
            raw = params["track_indexes"]
            if not isinstance(raw, list):
                raise ValueError("track_indexes must be a list")
            indexes = [self._require_int(item, "track_indexes") for item in raw]
        disarm = False
        if "disarm" in params or "disarmed" in params:
            disarm = self._require_bool(
                self._pick_alias(params, ("disarm", "disarmed"), True, "disarm"),
                "disarm",
            )
        if not callable(getattr(self._song, "stop_playing", None)):
            raise ValueError("Song has no stop_playing; nothing was changed")
        self._song.stop_playing()
        record_mode, _changed = self._assign_song_bool("record_mode", False)
        disarmed = []
        if disarm:
            if indexes is None:
                chosen = [
                    (index, track)
                    for index, track in enumerate(self._iter_tracks())
                    if self._safe_arm(track)
                ]
            else:
                chosen = self._arrangement_targets(indexes)
            for index, track in chosen:
                if self._can_be_armed(track) and self._safe_arm(track):
                    track.arm = False
                    disarmed.append(index)
        discarded = []
        if self._optional_flag(params, "discard_current", False):
            if indexes is None:
                tracks = list(enumerate(self._iter_tracks()))
            else:
                tracks = self._arrangement_targets(indexes)
            for index, track in tracks:
                for item in self._discard_recording_on_track(track):
                    item["track_index"] = index
                    discarded.append(item)
        return {
            "requested_playing": False,
            "requested_record_mode": False,
            "record_mode_written": record_mode,
            "disarmed_tracks": disarmed,
            "discarded": discarded,
            "deleted": bool(discarded),
            "note": (
                "Stop was requested. playing and record_mode in the outer "
                "reply are the requested state after the next tick read. "
                "Finished takes are not deleted."
            ),
        }

    def _plan_mixer_item(self, item):
        if not isinstance(item, dict):
            raise ValueError("must be an object")
        allowed = (
            "track_index", "volume", "value", "pan", "panning",
            "mute", "solo", "arm", "armed", "sends",
        )
        unknown = [key for key in item.keys() if key not in allowed]
        if unknown:
            raise ValueError("unknown fields %s" % ", ".join(sorted(str(k) for k in unknown)))
        track_index = self._require_present_int(item, "track_index")
        track = self._track_at(track_index)
        plan = {
            "track_index": track_index,
            "track": track,
            "volume": None,
            "panning": None,
            "mute": None,
            "solo": None,
            "arm": None,
            "sends": [],
        }
        if "volume" in item or "value" in item:
            raw = self._pick_alias(item, ("volume", "value"), True, "volume")
            self._check_param_range(self._mixer(track).volume, raw, "volume")
            plan["volume"] = self._finite_float(raw, "volume")
        if "pan" in item or "panning" in item:
            raw = self._pick_alias(item, ("pan", "panning"), True, "panning")
            self._check_param_range(self._mixer(track).panning, raw, "panning")
            plan["panning"] = self._finite_float(raw, "panning")
        for attr in ("mute", "solo"):
            if attr in item:
                plan[attr] = self._require_bool(item[attr], attr)
        if "arm" in item or "armed" in item:
            plan["arm"] = self._require_bool(
                self._pick_alias(item, ("arm", "armed"), True, "arm"), "arm"
            )
            self._assert_can_arm(track)
        if "sends" in item:
            sends = list(self._mixer(track).sends)
            normalized = self._normalize_sends(item["sends"])
            checked = []
            for index, value in normalized:
                if index < 0 or index >= len(sends):
                    raise ValueError(
                        "send_index out of range (track has %d sends)" % len(sends)
                    )
                self._check_param_range(sends[index], value, "send")
                checked.append((index, self._finite_float(value, "send")))
            plan["sends"] = checked
        return plan

    def _apply_mixer_item(self, plan):
        track = plan["track"]
        changed = False
        mixer = None
        if plan["volume"] is not None or plan["panning"] is not None or plan["sends"]:
            mixer = self._mixer(track)
        if plan["volume"] is not None:
            _value, wrote = self._assign_param(mixer.volume, plan["volume"], "volume")
            changed = changed or wrote
        if plan["panning"] is not None:
            _value, wrote = self._assign_param(mixer.panning, plan["panning"], "panning")
            changed = changed or wrote
        if plan["sends"]:
            sends = list(mixer.sends)
            for index, value in plan["sends"]:
                _value, wrote = self._assign_param(sends[index], value, "send")
                changed = changed or wrote
        for attr in ("mute", "solo", "arm"):
            if plan[attr] is not None:
                _value, wrote = self._assign_bool_attr(track, attr, plan[attr])
                changed = changed or wrote
        state = self._mixer_state(plan["track_index"], track)
        state["changed"] = changed
        state["error"] = None
        return state

    def _cmd_set_mixer_batch(self, params):
        self._reject_unknown(params, ("tracks",))
        items = params.get("tracks")
        if not isinstance(items, list):
            raise ValueError("tracks must be a list")
        plans = []
        for position, item in enumerate(items):
            try:
                plans.append(self._plan_mixer_item(item))
            except Exception as e:
                raise ValueError(
                    "tracks[%d]: %s Nothing was changed." % (position, e)
                )
        results = [self._apply_mixer_item(plan) for plan in plans]
        return {
            "tracks": results,
            "changed": any(item.get("changed") for item in results),
        }

    def _cmd_set_metronome(self, params):
        self._reject_unknown(params, ("enabled", "on"))
        enabled = self._enabled_alias(params)
        value, changed = self._assign_song_bool("metronome", enabled)
        return {"metronome": value, "changed": changed}

    def _cmd_set_count_in(self, params):
        """Try Song.count_in_duration. The Live 12.4.5 LOM lists it as get/observe."""
        self._reject_unknown(params, ("bars",))
        if "bars" not in params:
            raise ValueError("bars is required")
        bars = self._require_int(params["bars"], "bars") if not isinstance(params["bars"], float) else None
        if bars is None:
            bars_f = self._finite_float(params["bars"], "bars")
            if bars_f != int(bars_f):
                raise ValueError("bars must be 0, 1, 2, or 4")
            bars = int(bars_f)
        mapping = {0: 0, 1: 1, 2: 2, 4: 3}
        if bars not in mapping:
            raise ValueError(
                "count-in bars must be 0, 1, 2, or 4. "
                "Other lengths need pre_roll_bars on quick_record or record_loop."
            )
        current = self._optional_song_value("count_in_duration", int, None)
        writable = False
        readback = current
        error = None
        try:
            self._song.count_in_duration = mapping[bars]
            readback = self._optional_song_value("count_in_duration", int, None)
            writable = readback == mapping[bars]
        except Exception as e:
            error = str(e)
            writable = False
        result = {
            "count_in_writable": writable,
            "count_in_duration": readback,
            "requested_bars": bars,
            "requested_index": mapping[bars],
        }
        if not writable:
            result["note"] = (
                "Song.count_in_duration is read-only in the Live 12.4.5 "
                "object model (get/observe). It was not changed. Use "
                "pre_roll_bars on quick_record or record_loop to start the "
                "metronome and record after N bars."
            )
            if error:
                result["error"] = error
        return result

    def _cmd_create_scene(self, params):
        self._reject_unknown(params, ("index",))
        index = -1
        if "index" in params and params["index"] is not None:
            index = self._require_int(params["index"], "index")
        create = getattr(self._song, "create_scene", None)
        if not callable(create):
            raise ValueError("This Live song cannot create a scene")
        create(index)
        scenes = list(self._song.scenes)
        new_index = len(scenes) - 1 if index < 0 else index
        if new_index < 0 or new_index >= len(scenes):
            new_index = len(scenes) - 1
        scene = scenes[new_index]
        return {
            "index": new_index,
            "name": getattr(scene, "name", ""),
            "scene_count": len(scenes),
        }

    def _take_slots(self, track):
        found = []
        for index, slot in enumerate(list(track.clip_slots)):
            if not bool(getattr(slot, "has_clip", False)):
                continue
            clip = slot.clip
            name = str(getattr(clip, "name", ""))
            length_info = self._length_report(clip)
            found.append({
                "clip_index": index,
                "take": len(found) + 1,
                "name": name,
                "keep": name.rstrip().endswith("KEEP"),
                "is_recording": bool(getattr(clip, "is_recording", False)) or self._slot_is_recording(slot),
                "length": length_info.get("length"),
                "length_status": length_info.get("length_status"),
            })
        return found

    def _cmd_list_takes(self, params):
        self._reject_unknown(params, ("track_index",))
        track_index = self._require_present_int(params, "track_index")
        track = self._track_at(track_index)
        takes = self._take_slots(track)
        return {"track_index": track_index, "name": getattr(track, "name", ""), "takes": takes}

    def _cmd_keep_take(self, params):
        self._reject_unknown(params, ("track_index", "take", "clip_index"))
        track_index = self._require_present_int(params, "track_index")
        track = self._track_at(track_index)
        takes = self._take_slots(track)
        if "clip_index" in params:
            clip_index = self._require_int(params["clip_index"], "clip_index")
        elif "take" in params:
            number = self._require_int(params["take"], "take")
            match = None
            for item in takes:
                if item["take"] == number:
                    match = item
                    break
            if match is None:
                raise ValueError(
                    "No take %s on track %s. Nothing was renamed." % (number, track_index)
                )
            clip_index = match["clip_index"]
        else:
            raise ValueError("take (1-based) or clip_index is required")
        slot = self._slot_at(track, clip_index)
        if not bool(getattr(slot, "has_clip", False)):
            raise ValueError("No clip in that slot. Nothing was renamed.")
        if self._slot_is_recording(slot):
            raise ValueError("That clip is still recording. Nothing was renamed.")
        clip = slot.clip
        name = str(getattr(clip, "name", "") or "")
        if not name.rstrip().endswith("KEEP"):
            clip.name = (name + " KEEP").strip() if name else "KEEP"
        color_set = False
        try:
            clip.color = 0x0000C853
            color_set = True
        except Exception:
            color_set = False
        return {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": str(clip.name),
            "color_set": color_set,
            "deleted": False,
        }

    def _cmd_stop_take(self, params):
        self._reject_unknown(params, ("track_index", "discard_current"))
        discard = self._optional_flag(params, "discard_current", True)
        track_index = None
        if "track_index" in params and params["track_index"] is not None:
            track_index = self._require_int(params["track_index"], "track_index")
        discarded = []
        stopped = []
        tracks = (
            [(track_index, self._track_at(track_index))]
            if track_index is not None
            else list(enumerate(self._iter_tracks()))
        )
        for index, track in tracks:
            recording = self._recording_slot_indexes(track)
            if not recording:
                continue
            if discard:
                for item in self._discard_recording_on_track(track):
                    item["track_index"] = index
                    discarded.append(item)
            else:
                self._slot_at(track, recording[0]).stop()
                stopped.append({"track_index": index, "clip_indexes": recording})
        return {
            "discard_current": discard,
            "discarded": discarded,
            "stopped": stopped,
            "deleted": bool(discarded),
            "note": (
                "Only a clip that was recording when this command ran is "
                "deleted. Finished takes were left in place."
            ),
        }

    def _cmd_record_loop(self, params):
        self._reject_unknown(params, (
            "track_index", "bars", "max_takes", "monitoring", "pre_roll_bars",
            "arm", "armed",
        ))
        track_index = self._require_present_int(params, "track_index")
        if "bars" not in params:
            raise ValueError("bars is required")
        beats, _numer = self._bars_to_beats(params["bars"])
        max_takes = None
        if "max_takes" in params and params["max_takes"] is not None:
            max_takes = self._require_int(params["max_takes"], "max_takes")
            if max_takes < 1:
                raise ValueError("max_takes must be >= 1")
        arm = True
        if "arm" in params or "armed" in params:
            arm = self._require_bool(
                self._pick_alias(params, ("arm", "armed"), True, "arm"), "arm"
            )
        track = self._track_at(track_index)
        if self._is_frozen(track) or self._is_group_track(track):
            raise ValueError("Track cannot host record_loop; nothing was fired")
        self._assert_can_arm(track)
        loops = self._record_loops()
        if track_index in loops and not loops[track_index].get("stopped", True):
            raise ValueError("A record loop is already running on this track")
        state = {
            "track_index": track_index,
            "bars": params["bars"],
            "beats": beats,
            "max_takes": max_takes,
            "takes_started": 0,
            "stopped": False,
            "slots": [],
            "arm": arm,
        }
        loops[track_index] = state
        self._apply_recording_monitoring(track, params.get("monitoring", None))
        pre_roll = self._optional_pre_roll(params)

        def start_first():
            if state["stopped"]:
                return
            self._record_loop_fire_next(state)

        if pre_roll:
            target = self._begin_pre_roll(pre_roll)
            holder = {"tries": 0}

            def wait():
                if state["stopped"]:
                    return
                holder["tries"] += 1
                if holder["tries"] > 400:
                    state["stopped"] = True
                    return
                try:
                    now = float(self._song.current_song_time)
                except Exception:
                    now = 0.0
                if now + 1e-4 < target:
                    self._schedule_later(1, wait)
                    return
                start_first()

            self._schedule_later(1, wait)
            launched = False
        else:
            start_first()
            launched = True
        return {
            "track_index": track_index,
            "bars": params["bars"],
            "record_length": beats,
            "max_takes": max_takes if max_takes is not None else _RECORD_LOOP_HARD_CAP,
            "max_takes_is_default_cap": max_takes is None,
            "started": launched,
            "pre_roll_bars": pre_roll or None,
            "clip_index": state["slots"][-1] if state["slots"] else None,
            "arm": self._safe_arm(track),
            "note": (
                "record_loop returns immediately. Later takes are fired from "
                "later main-thread ticks when the current take stops "
                "recording. The main thread is not blocked. Stop with "
                "stop_record_loop. Without max_takes the loop stops at "
                "%d takes." % _RECORD_LOOP_HARD_CAP
            ),
        }

    def _record_loop_fire_next(self, state):
        if state.get("stopped"):
            return
        limit = state["max_takes"] if state["max_takes"] is not None else _RECORD_LOOP_HARD_CAP
        if state["takes_started"] >= limit:
            state["stopped"] = True
            return
        track = self._track_at(state["track_index"])
        slot_index = self._first_empty_slot_index(track)
        if slot_index is None:
            slot_index = self._append_scene_and_slot(track)
        slot = self._slot_at(track, slot_index)
        if bool(getattr(slot, "has_clip", False)) or self._slot_is_recording(slot):
            state["stopped"] = True
            return
        self._fire_empty_slot(
            track, state["track_index"], slot, slot_index, state["beats"], state["arm"]
        )
        state["takes_started"] += 1
        state["slots"].append(slot_index)
        self._watch_loop_slot(state, slot)

    def _watch_loop_slot(self, state, slot):
        holder = {"tries": 0, "saw_recording": False, "listener": None, "clip": None}

        def finished():
            clip = holder["clip"]
            listener = holder["listener"]
            if clip is not None and listener is not None:
                try:
                    clip.remove_is_recording_listener(listener)
                except Exception:
                    pass
                holder["listener"] = None
            if state.get("stopped"):
                return
            self._record_loop_fire_next(state)

        def on_change():
            try:
                if holder["clip"] is not None and not bool(holder["clip"].is_recording):
                    finished()
            except Exception:
                finished()

        def poll():
            if state.get("stopped") or holder["listener"] is not None:
                return
            holder["tries"] += 1
            if holder["tries"] > 800:
                state["stopped"] = True
                return
            try:
                recording = self._slot_is_recording(slot)
                if recording:
                    holder["saw_recording"] = True
                    if bool(getattr(slot, "has_clip", False)):
                        clip = slot.clip
                        holder["clip"] = clip
                        if callable(getattr(clip, "add_is_recording_listener", None)):
                            clip.add_is_recording_listener(on_change)
                            holder["listener"] = on_change
                            return
                elif holder["saw_recording"]:
                    finished()
                    return
            except Exception:
                pass
            self._schedule_later(1, poll)

        self._schedule_later(1, poll)

    def _cmd_stop_record_loop(self, params):
        self._reject_unknown(params, ("track_index", "discard_current"))
        track_index = self._require_present_int(params, "track_index")
        discard = self._optional_flag(params, "discard_current", True)
        loops = self._record_loops()
        state = loops.get(track_index)
        if state is not None:
            state["stopped"] = True
        track = self._track_at(track_index)
        discarded = self._discard_recording_on_track(track) if discard else []
        if not discard:
            recording = self._recording_slot_indexes(track)
            if recording:
                self._slot_at(track, recording[0]).stop()
        if state is not None:
            loops.pop(track_index, None)
        return {
            "track_index": track_index,
            "stopped": True,
            "discard_current": discard,
            "discarded": discarded,
            "deleted": bool(discarded),
            "finished_takes_kept": True,
            "note": (
                "The loop will not start another take. Only the clip that "
                "was recording is deleted when discard_current is true."
            ),
        }

    def _performance(self):
        state = getattr(self, "_performance_state", None)
        if state is None:
            state = {
                "current_take": None,
                "last_full_take": None,
            }
            self._performance_state = state
        return state

    def _normalize_view_name(self, name):
        if not isinstance(name, str):
            raise ValueError("view must be session or arrangement")
        key = self._fold_text(name.strip())
        if key in ("session",):
            return "session"
        if key in ("arrangement", "arranger"):
            return "arrangement"
        raise ValueError("view must be session or arrangement")

    def _shown_view(self):
        try:
            view = self.application().view
            raw = getattr(view, "focused_document_view", None)
            if raw is None and callable(getattr(view, "is_view_visible", None)):
                if view.is_view_visible("Arranger"):
                    return "arrangement"
                return "session"
            return self._normalize_view_name(raw or "Session")
        except Exception:
            return "session"

    def _cmd_show_view(self, params):
        self._reject_unknown(params, ("view",))
        if "view" not in params:
            raise ValueError("view is required")
        target = self._normalize_view_name(params["view"])
        live_name = "Session" if target == "session" else "Arranger"
        app = self.application()
        if app is None or not hasattr(app, "view"):
            raise ValueError("Live application view is not available")
        app.view.show_view(live_name)
        focus = getattr(app.view, "focus_view", None)
        if callable(focus):
            try:
                focus(live_name)
            except Exception as e:
                self.log_message("focus_view failed: " + str(e))
        try:
            app.view.focused_document_view = live_name
        except Exception:
            pass
        return {"view": self._shown_view(), "shown": live_name}

    def _match_track_name(self, name):
        if not isinstance(name, str) or not name.strip():
            raise ValueError("track name must be a non-empty string")
        wanted = self._fold_text(name.strip())
        hits = []
        for index, track in enumerate(self._iter_tracks()):
            if self._fold_text(str(getattr(track, "name", ""))) == wanted:
                hits.append((index, track))
        if not hits:
            raise ValueError("No track named %r" % name)
        if len(hits) > 1:
            raise ValueError("More than one track named %r" % name)
        return hits[0]

    def _cmd_arm_tracks(self, params):
        self._reject_unknown(params, ("names", "exclusive", "monitoring"))
        names = params.get("names")
        if not isinstance(names, list) or not names:
            raise ValueError("names must be a non-empty list of track names")
        exclusive = self._optional_flag(params, "exclusive", True)
        matched = [self._match_track_name(name) for name in names]
        matched_indexes = set(index for index, _track in matched)
        if exclusive:
            for index, track in enumerate(self._iter_tracks()):
                if index not in matched_indexes and self._safe_arm(track):
                    if self._can_be_armed(track):
                        track.arm = False
        armed = []
        for index, track in matched:
            self._assert_can_arm(track)
            if not self._safe_arm(track):
                track.arm = True
            monitoring = params.get("monitoring", None)
            self._apply_recording_monitoring(track, monitoring)
            armed.append({"index": index, "name": str(track.name), "arm": True})
        return {"exclusive": exclusive, "armed_tracks": armed}

    def _armed_pairs(self):
        pairs = []
        for index, track in enumerate(self._iter_tracks()):
            if self._safe_arm(track):
                pairs.append((index, track))
        return pairs

    def _cmd_get_performance_state(self, params):
        self._reject_unknown(params, ())
        perf = self._performance()
        recording_slots = []
        for index, track in enumerate(self._iter_tracks()):
            for clip_index in self._recording_slot_indexes(track):
                recording_slots.append({
                    "track_index": index,
                    "clip_index": clip_index,
                })
        return {
            "view": self._shown_view(),
            "armed_tracks": [
                {"index": index, "name": str(track.name)}
                for index, track in self._armed_pairs()
            ],
            "recording": bool(recording_slots) or self._read_song_bool("record_mode"),
            "recording_slots": recording_slots,
            "record_mode": self._optional_song_value("record_mode", bool, None),
            "tempo": self._optional_song_value("tempo", float, None),
            "metronome": self._optional_song_value("metronome", bool, None),
            "is_playing": self._optional_song_value("is_playing", bool, None),
            "current_song_time": self._optional_song_value("current_song_time", float, None),
            "loop": self._optional_song_value("loop", bool, None),
            "last_take": perf.get("last_full_take"),
            "current_take": perf.get("current_take"),
        }

    def _engage_count_in(self, bars):
        mapping = {0: 0, 1: 1, 2: 2, 4: 3}
        index = mapping.get(int(bars), 1)
        try:
            self._song.count_in_duration = index
            readback = self._optional_song_value("count_in_duration", int, None)
            if readback == index:
                try:
                    self._song.metronome = True
                except Exception:
                    pass
                return {
                    "path": "count_in_duration",
                    "bars": int(bars),
                    "count_in_writable": True,
                }
            error = "write did not stick"
        except Exception as e:
            error = str(e)
        return {
            "path": "pre_roll",
            "bars": int(bars),
            "count_in_writable": False,
            "error": error,
        }

    def _common_empty_slot(self, pairs):
        if not pairs:
            return None
        try:
            limit = min(len(list(track.clip_slots)) for _index, track in pairs)
        except Exception:
            return None
        for slot_index in range(limit):
            empty = True
            for _index, track in pairs:
                slot = self._slot_at(track, slot_index)
                if bool(getattr(slot, "has_clip", False)) or self._slot_is_recording(slot):
                    empty = False
                    break
            if empty:
                return slot_index
        return None

    def _remember_take(self, take):
        perf = self._performance()
        perf["current_take"] = take
        return take

    def _mark_take_full_later(self, take):
        def poll():
            if take.get("cancelled"):
                return
            still = False
            for item in take.get("slots") or []:
                track = self._track_at(item["track_index"])
                slot = self._slot_at(track, item["clip_index"])
                if self._slot_is_recording(slot):
                    still = True
                    break
            if take.get("view") == "arrangement" and self._read_song_bool("record_mode"):
                if not take.get("loop"):
                    try:
                        now = float(self._song.current_song_time)
                    except Exception:
                        now = 0.0
                    if now + 1e-3 < float(take.get("end_time") or 0.0):
                        still = True
                    else:
                        self._assign_song_bool("record_mode", False)
            if still:
                self._schedule_later(1, poll)
                return
            take["full"] = True
            take["recording"] = False
            self._performance()["last_full_take"] = take
            if self._performance().get("current_take") is take:
                self._performance()["current_take"] = None
        self._schedule_later(1, poll)

    def _start_session_take(self, pairs, beats, bars, count_in, loop):
        slot_index = self._common_empty_slot(pairs)
        created_scene = False
        if slot_index is None:
            self._append_scene_and_slot(pairs[0][1])
            slot_index = self._common_empty_slot(pairs)
            created_scene = True
            if slot_index is None:
                raise ValueError("Could not find an empty scene row. Nothing was fired.")
        take = {
            "view": "session",
            "bars": bars,
            "beats": beats,
            "loop": False,
            "slots": [
                {"track_index": index, "clip_index": slot_index, "name": str(track.name)}
                for index, track in pairs
            ],
            "recording": True,
            "full": False,
            "count_in": count_in,
            "created_scene": created_scene,
        }

        def fire_all():
            for index, track in pairs:
                slot = self._slot_at(track, slot_index)
                if bool(getattr(slot, "has_clip", False)) or self._slot_is_recording(slot):
                    continue
                self._fire_empty_slot(track, index, slot, slot_index, beats, True)
            self._mark_take_full_later(take)

        if count_in and count_in.get("path") == "pre_roll":
            target = self._begin_pre_roll(count_in["bars"])
            holder = {"tries": 0}

            def wait():
                holder["tries"] += 1
                if holder["tries"] > 400:
                    return
                try:
                    now = float(self._song.current_song_time)
                except Exception:
                    now = 0.0
                if now + 1e-4 < target:
                    self._schedule_later(1, wait)
                    return
                fire_all()

            self._schedule_later(1, wait)
            take["recording"] = False
            take["scheduled"] = True
        else:
            fire_all()
            take["scheduled"] = False
        self._remember_take(take)
        return {
            "take": take,
            "slot_index": slot_index,
            "count_in": count_in,
            "deleted": False,
            "note": (
                "Session take uses one empty scene row on every armed track. "
                "Fixed length is exact for this fire only. Count-in path is "
                "in count_in.path."
            ),
        }

    def _start_arrangement_take(self, pairs, beats, bars, count_in, loop):
        try:
            if bool(self._song.loop):
                start = float(self._song.loop_start)
            else:
                start = float(self._song.current_song_time)
        except Exception:
            start = 0.0
        end = start + float(beats)
        try:
            self._song.loop_start = start
            self._song.loop_length = float(beats)
        except Exception as e:
            raise ValueError("Could not set the arrangement loop brace (%s)" % e)
        if loop:
            self._assign_song_bool("loop", True)
            try:
                self._song.punch_in = False
                self._song.punch_out = False
            except Exception:
                pass
        else:
            self._assign_song_bool("loop", False)
            try:
                self._song.punch_in = True
                self._song.punch_out = True
            except Exception as e:
                self.log_message("punch in/out unavailable: " + str(e))
        take = {
            "view": "arrangement",
            "bars": bars,
            "beats": beats,
            "loop": bool(loop),
            "start_time": start,
            "end_time": end,
            "tracks": [
                {"track_index": index, "name": str(track.name)}
                for index, track in pairs
            ],
            "recording": True,
            "full": False,
            "count_in": count_in,
            "take_lanes": self._lanes_for_pairs(pairs),
            "lane_note": self._take_lane_support_note(),
        }

        def roll():
            self._assign_song_bool("record_mode", True)
            if callable(getattr(self._song, "start_playing", None)):
                if not self._read_song_bool("is_playing"):
                    self._song.start_playing()
            self._mark_take_full_later(take)

        if count_in and count_in.get("path") == "pre_roll":
            target = self._begin_pre_roll(count_in["bars"])
            holder = {"tries": 0}

            def wait():
                holder["tries"] += 1
                if holder["tries"] > 400:
                    return
                try:
                    now = float(self._song.current_song_time)
                except Exception:
                    now = 0.0
                if now + 1e-4 < target:
                    self._schedule_later(1, wait)
                    return
                roll()

            self._schedule_later(1, wait)
            take["scheduled"] = True
            take["recording"] = False
        else:
            roll()
            take["scheduled"] = False
        self._remember_take(take)
        return {
            "take": take,
            "count_in": count_in,
            "deleted": False,
            "note": (
                "Arrangement take sets the loop brace to this length. "
                "Loop mode leaves the brace looping so Live can open a new "
                "take lane on each pass when comping is enabled in Live. "
                "A one-shot uses punch-in and punch-out on that brace. "
                "The LOM cannot activate or delete a take lane; see take_lanes."
            ),
        }

    def _cmd_take(self, params):
        self._reject_unknown(params, (
            "bars", "loop", "count_in", "names", "exclusive", "view",
        ))
        bars = DEFAULT_TAKE_BARS
        if "bars" in params and params["bars"] is not None:
            bars = params["bars"]
        beats, _numer = self._bars_to_beats(bars)
        loop = self._optional_flag(params, "loop", False)
        count_flag = True
        if "count_in" in params and params["count_in"] is not None:
            count_flag = self._require_bool(params["count_in"], "count_in")
        if "names" in params:
            self._cmd_arm_tracks({
                "names": params["names"],
                "exclusive": params.get("exclusive", True),
            })
        if "view" in params and params["view"] is not None:
            view = self._normalize_view_name(params["view"])
            self._cmd_show_view({"view": view})
        else:
            view = self._shown_view()
        pairs = self._armed_pairs()
        if not pairs:
            raise ValueError("No armed tracks. Nothing was recorded.")
        count_in = self._engage_count_in(DEFAULT_COUNT_IN_BARS) if count_flag else {
            "path": "off", "bars": 0, "count_in_writable": None,
        }
        if view == "arrangement":
            result = self._start_arrangement_take(pairs, beats, bars, count_in, loop)
        else:
            result = self._start_session_take(pairs, beats, bars, count_in, loop)
        result["view"] = view
        result["bars"] = bars
        result["beats"] = beats
        return result

    def _delete_session_slots(self, slots, only_recording):
        deleted = []
        for item in slots or []:
            track = self._track_at(item["track_index"])
            slot = self._slot_at(track, item["clip_index"])
            recording = self._slot_is_recording(slot)
            if only_recording and not recording:
                continue
            if not only_recording and recording:
                continue
            if not bool(getattr(slot, "has_clip", False)) and not recording:
                continue
            name = ""
            try:
                if bool(getattr(slot, "has_clip", False)):
                    name = str(slot.clip.name)
            except Exception:
                name = ""
            if recording and callable(getattr(slot, "stop", None)):
                try:
                    slot.stop()
                except Exception:
                    pass
            if bool(getattr(slot, "has_clip", False)) and callable(getattr(slot, "delete_clip", None)):
                slot.delete_clip()
            deleted.append({
                "track_index": item["track_index"],
                "clip_index": item["clip_index"],
                "name": name,
            })
        return deleted

    def _cmd_again(self, params):
        self._reject_unknown(params, ())
        perf = self._performance()
        current = perf.get("current_take")
        deleted = []
        if current and current.get("view") == "session":
            deleted = self._delete_session_slots(current.get("slots"), only_recording=False)
            current["cancelled"] = True
        elif current and current.get("recording"):
            self._assign_song_bool("record_mode", False)
            current["cancelled"] = True
        elif perf.get("last_full_take"):
            full = perf["last_full_take"]
            if full.get("view") == "session":
                deleted = self._delete_session_slots(full.get("slots"), only_recording=False)
            full["cancelled"] = True
            perf["last_full_take"] = None
        perf["current_take"] = None
        started = self._cmd_take({})
        started["deleted_previous"] = deleted
        return started

    def _cmd_scrap(self, params):
        self._reject_unknown(params, ())
        perf = self._performance()
        full = perf.get("last_full_take")
        if not full:
            return {
                "deleted": False,
                "note": "No finished take to scrap. The take that is still recording is left for cancel.",
            }
        deleted = []
        if full.get("view") == "session":
            deleted = self._delete_session_slots(full.get("slots"), only_recording=False)
        full["cancelled"] = True
        perf["last_full_take"] = None
        return {"deleted": bool(deleted), "clips": deleted, "moved": False}

    def _cmd_cancel(self, params):
        self._reject_unknown(params, ())
        perf = self._performance()
        current = perf.get("current_take")
        if current:
            current["cancelled"] = True
        discarded = []
        for index, track in enumerate(self._iter_tracks()):
            for item in self._discard_recording_on_track(track):
                item["track_index"] = index
                discarded.append(item)
        self._assign_song_bool("record_mode", False)
        self._assign_song_bool("session_record", False)
        try:
            self._song.punch_in = False
            self._song.punch_out = False
        except Exception:
            pass
        if callable(getattr(self._song, "stop_playing", None)):
            self._song.stop_playing()
        for state in list(self._record_loops().values()):
            state["stopped"] = True
        self._record_loops().clear()
        perf["current_take"] = None
        return {
            "deleted": bool(discarded),
            "discarded": discarded,
            "playing": False,
            "record_mode": False,
            "finished_takes_kept": True,
            "moved": False,
        }

    def _cmd_play_transport(self, params):
        self._reject_unknown(params, ())
        self._assign_song_bool("record_mode", False)
        self._assign_song_bool("session_record", False)
        if callable(getattr(self._song, "continue_playing", None)):
            self._song.continue_playing()
        elif callable(getattr(self._song, "start_playing", None)):
            self._song.start_playing()
        else:
            raise ValueError("Song cannot start playback")
        return {
            "playing": True,
            "record_mode": False,
            "deleted": False,
            "note": "Transport play without recording. Requested state, not a same-tick read.",
        }

    def _cmd_pause_transport(self, params):
        self._reject_unknown(params, ())
        self._assign_song_bool("record_mode", False)
        if callable(getattr(self._song, "stop_playing", None)):
            self._song.stop_playing()
        return {
            "playing": False,
            "record_mode": False,
            "deleted": False,
            "note": "Transport pause without deleting takes. Requested state.",
        }

    def _cmd_nudge_tempo(self, params):
        self._reject_unknown(params, ("delta",))
        delta = 5.0
        if "delta" in params and params["delta"] is not None:
            delta = self._finite_float(params["delta"], "delta")
        current = float(self._song.tempo)
        updated = current + delta
        if updated < 20.0:
            updated = 20.0
        if updated > 999.0:
            updated = 999.0
        self._song.tempo = updated
        return {"tempo": float(self._song.tempo), "previous": current, "delta": delta}

    def _cmd_mark_playhead(self, params):
        self._reject_unknown(params, ("name",))
        name = params.get("name") or ""
        if name and not isinstance(name, str):
            raise ValueError("name must be a string")
        song = self._song
        if not callable(getattr(song, "set_or_delete_cue", None)):
            raise ValueError("This Live song cannot create a locator")
        target = float(song.current_song_time)
        existing = None
        for cue in list(getattr(song, "cue_points", []) or []):
            try:
                if abs(float(cue.time) - target) < 1e-3:
                    existing = cue
                    break
            except Exception:
                continue
        if existing is None:
            song.set_or_delete_cue()
            for cue in list(getattr(song, "cue_points", []) or []):
                try:
                    if abs(float(cue.time) - target) < 1e-3:
                        existing = cue
                        break
                except Exception:
                    continue
        if existing is None:
            raise ValueError("Locator was not created at the playhead")
        if name:
            try:
                existing.name = name
            except Exception as e:
                self.log_message("locator rename failed: " + str(e))
        return {
            "time": float(existing.time),
            "name": str(getattr(existing, "name", "")),
            "moved_playhead": False,
        }

    def _take_lane_support_note(self):
        return (
            "Live 12.4.5 TakeLane can be listed and renamed. "
            "It exposes arrangement_clips, name, create_audio_clip, and "
            "create_midi_clip. Track.create_take_lane exists. "
            "No member activates a lane or deletes one. "
            "Per-pass lanes during a looping arrangement record are Live's "
            "comping behavior, not a call this script can make."
        )

    def _lanes_for_pairs(self, pairs):
        listed = []
        for index, track in pairs:
            lanes = self._read_take_lanes(track)
            listed.append({
                "track_index": index,
                "name": str(getattr(track, "name", "")),
                "take_lanes": lanes,
                "delete_supported": self._take_lane_delete_callable(track, lanes)[0],
                "activate_supported": False,
            })
        return listed

    def _read_take_lanes(self, track):
        raw = getattr(track, "take_lanes", None)
        if raw is None:
            return None
        lanes = []
        try:
            sequence = list(raw)
        except Exception as e:
            return [{"error": str(e)}]
        for index, lane in enumerate(sequence):
            clips = []
            try:
                for clip in list(getattr(lane, "arrangement_clips", []) or []):
                    clips.append({
                        "name": str(getattr(clip, "name", "")),
                        "start_time": float(getattr(clip, "start_time", 0.0)),
                        "length": float(getattr(clip, "length", 0.0)),
                        "is_recording": bool(getattr(clip, "is_recording", False)),
                    })
            except Exception as e:
                clips.append({"error": str(e)})
            lanes.append({
                "index": index,
                "name": str(getattr(lane, "name", "")),
                "clips": clips,
            })
        return lanes

    def _take_lane_delete_callable(self, track, lanes):
        if callable(getattr(track, "delete_take_lane", None)):
            return True, "track.delete_take_lane"
        if lanes:
            return False, None
        return False, None

    def _track_from_name_or_index(self, params):
        if "track_index" in params and params["track_index"] is not None:
            index = self._require_int(params["track_index"], "track_index")
            return index, self._track_at(index)
        if "name" in params and params["name"]:
            return self._match_track_name(params["name"])
        raise ValueError("track_index or name is required")

    def _cmd_list_take_lanes(self, params):
        self._reject_unknown(params, ("track_index", "name"))
        index, track = self._track_from_name_or_index(params)
        lanes = self._read_take_lanes(track)
        supported = lanes is not None
        delete_supported, _how = self._take_lane_delete_callable(track, lanes or [])
        return {
            "track_index": index,
            "name": str(getattr(track, "name", "")),
            "supported": supported,
            "take_lanes": lanes if lanes is not None else [],
            "delete_supported": delete_supported,
            "activate_supported": False,
            "note": self._take_lane_support_note(),
        }

    def _cmd_label_take_lane(self, params):
        self._reject_unknown(params, ("track_index", "name", "index", "label"))
        index, track = self._track_from_name_or_index(params)
        raw = getattr(track, "take_lanes", None)
        if raw is None:
            return {
                "supported": False,
                "note": self._take_lane_support_note(),
            }
        lanes = list(raw)
        if not lanes:
            raise ValueError("Track has no take lanes. Nothing was renamed.")
        lane_index = len(lanes) - 1
        if "index" in params and params["index"] is not None:
            lane_index = self._require_int(params["index"], "index")
        if lane_index < 0 or lane_index >= len(lanes):
            raise ValueError("Take lane index out of range")
        lane = lanes[lane_index]
        label = params.get("label") or "KEEP"
        if not isinstance(label, str):
            raise ValueError("label must be a string")
        current = str(getattr(lane, "name", "") or "")
        if label == "KEEP" and not current.rstrip().endswith("KEEP"):
            lane.name = (current + " KEEP").strip() if current else "KEEP"
        elif label != "KEEP":
            lane.name = label
        return {
            "track_index": index,
            "index": lane_index,
            "name": str(lane.name),
            "moved": False,
            "activate_supported": False,
        }

    def _cmd_delete_take_lane(self, params):
        self._reject_unknown(params, ("track_index", "name", "index"))
        index, track = self._track_from_name_or_index(params)
        raw = getattr(track, "take_lanes", None)
        if raw is None:
            return {"delete_supported": False, "deleted": False, "note": self._take_lane_support_note()}
        lanes = list(raw)
        if not lanes:
            return {"delete_supported": False, "deleted": False, "note": "No take lanes."}
        lane_index = len(lanes) - 1
        if "index" in params and params["index"] is not None:
            lane_index = self._require_int(params["index"], "index")
        deleter = getattr(track, "delete_take_lane", None)
        if callable(deleter):
            deleter(lane_index)
            return {"delete_supported": True, "deleted": True, "index": lane_index, "how": "track.delete_take_lane"}
        lane = lanes[lane_index]
        lane_delete = getattr(lane, "delete", None)
        if callable(lane_delete):
            lane_delete()
            return {"delete_supported": True, "deleted": True, "index": lane_index, "how": "lane.delete"}
        return {
            "delete_supported": False,
            "deleted": False,
            "index": lane_index,
            "note": self._take_lane_support_note(),
        }

    def _cmd_stop_performance(self, params):
        """Stop recording and delete only the clip that is being recorded."""
        self._reject_unknown(params, ())
        self._assign_song_bool("record_mode", False)
        self._assign_song_bool("session_record", False)
        discarded = []
        for index, track in enumerate(self._iter_tracks()):
            for item in self._discard_recording_on_track(track):
                item["track_index"] = index
                discarded.append(item)
        current = self._performance().get("current_take")
        if current:
            current["cancelled"] = True
            self._performance()["current_take"] = None
        return {
            "record_mode": False,
            "discarded": discarded,
            "deleted": bool(discarded),
            "playing": self._optional_song_value("is_playing", bool, None),
            "finished_takes_kept": True,
            "moved": False,
        }

    def _cmd_keep_last(self, params):
        """Append KEEP to the last finished take. Does not move clips."""
        self._reject_unknown(params, ())
        perf = self._performance()
        take = perf.get("last_full_take") or perf.get("current_take")
        labeled = []
        if take and take.get("view") == "session":
            for item in take.get("slots") or []:
                track = self._track_at(item["track_index"])
                slot = self._slot_at(track, item["clip_index"])
                if self._slot_is_recording(slot) or not bool(getattr(slot, "has_clip", False)):
                    continue
                clip = slot.clip
                name = str(getattr(clip, "name", "") or "")
                if not name.rstrip().endswith("KEEP"):
                    clip.name = (name + " KEEP").strip() if name else "KEEP"
                labeled.append({
                    "track_index": item["track_index"],
                    "clip_index": item["clip_index"],
                    "name": str(clip.name),
                })
        if not labeled:
            pairs = self._armed_pairs() or list(enumerate(self._iter_tracks()))[:1]
            for index, track in pairs:
                takes = self._take_slots(track)
                if not takes:
                    continue
                last = takes[-1]
                if last["is_recording"]:
                    continue
                slot = self._slot_at(track, last["clip_index"])
                clip = slot.clip
                name = str(getattr(clip, "name", "") or "")
                if not name.rstrip().endswith("KEEP"):
                    clip.name = (name + " KEEP").strip() if name else "KEEP"
                labeled.append({
                    "track_index": index,
                    "clip_index": last["clip_index"],
                    "name": str(clip.name),
                })
                break
        return {"labeled": labeled, "moved": False}
