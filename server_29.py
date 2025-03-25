#!/usr/bin/env python3
from flask import Flask, request, jsonify, render_template
import json, socket, os, glob, random, time, datetime
from concurrent.futures import ThreadPoolExecutor
import threading

app = Flask(__name__)

# -----------------------------------------------------------------------------
# Raspberry Pi Version:
#  - BASE_PATH is set to the current working directory.
#  - Channels are auto-detected: only subdirectories whose names include "channel"
#    (case-insensitive) from both the current working directory and /mnt/ssd/compressed_vids.
#  - Duration caching and parallel processing are used to speed up startup.
#  - Auto mode feature: every auto_interval seconds the video changes,
#      either by switching channels (global) or shuffling videos within the current channel (local).
#  - The Flask server binds to 0.0.0.0 so it's accessible from other devices on the network.
# -----------------------------------------------------------------------------

# Define base paths to search for channel folders.
BASE_PATHS = [os.getcwd(), "/mnt/myhdd/compressed_vids"]

# Auto-detect channels: include only subdirectories with "channel" in the name.
channels = {}
for base in BASE_PATHS:
    if os.path.exists(base):
        for entry in os.listdir(base):
            full_path = os.path.join(base, entry)
            if os.path.isdir(full_path) and "channel" in entry.lower():
                channels[entry] = full_path

print("Detected channels:", list(channels.keys()))

# Path to your transition video and its duration.
TRANSITION_VIDEO = os.path.abspath("transition.mp4")
TRANSITION_LENGTH = 3.0  # seconds

# Cache file for video durations.
CACHE_FILE = "durations_cache.json"

def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print("DEBUG: Error loading cache:", e)
            return {}
    return {}

def save_cache(cache):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(cache, f)
    except Exception as e:
        print("DEBUG: Error saving cache:", e)

# Global cache for durations.
durations_cache = load_cache()

def get_video_duration(video_path):
    """
    Uses ffprobe to get the duration of a video.
    """
    import subprocess
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return float(result.stdout.strip())
    except:
        return 0

def cached_get_video_duration(video_path):
    mod_time = os.path.getmtime(video_path)
    key = video_path
    if key in durations_cache and durations_cache[key]["mod_time"] == mod_time:
        return durations_cache[key]["duration"]
    else:
        duration = get_video_duration(video_path)
        durations_cache[key] = {"mod_time": mod_time, "duration": duration}
        return duration

# Data structures for playlists and durations.
channel_playlists = {}
channel_durations = {}

def init_playlists():
    """
    Scans each channel folder and builds playlists using parallel processing.
    """
    for ch, folder_path in channels.items():
        files = sorted(glob.glob(os.path.join(folder_path, "*.*")))
        random.shuffle(files)
        channel_playlists[ch] = files
        with ThreadPoolExecutor(max_workers=8) as executor:
            durations = list(executor.map(cached_get_video_duration, files))
        channel_durations[ch] = durations
        print(f"DEBUG: Found {len(files)} files in channel '{ch}':", files)
    save_cache(durations_cache)

init_playlists()

def get_seconds_since_midnight():
    """Return seconds elapsed since midnight."""
    now = datetime.datetime.now()
    midnight = datetime.datetime.combine(now.date(), datetime.time(0, 0, 0))
    return (now - midnight).total_seconds()

def compute_current_video_and_offset(channel):
    """
    Computes which video should be playing for a channel and its offset (in seconds)
    based on the current time.
    """
    playlist = channel_playlists.get(channel, [])
    durations = channel_durations.get(channel, [])
    if not playlist or not durations:
        return None, None
    total_duration = sum(durations)
    if total_duration == 0:
        return playlist[0], 0
    elapsed = get_seconds_since_midnight()
    channel_pos = elapsed % total_duration
    cumulative = 0
    for video, d in zip(playlist, durations):
        if cumulative + d > channel_pos:
            offset = channel_pos - cumulative
            return video, offset
        cumulative += d
    return playlist[-1], 0

# Global variable for current channel.
current_channel = None

def send_mpv_command(command):
    """
    Sends a JSON command to mpv via the IPC socket.
    """
    IPC_SOCKET = "/tmp/mpv-socket"
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(IPC_SOCKET)
        sock.sendall((json.dumps(command) + "\n").encode("utf-8"))
        response = sock.recv(1024)
        sock.close()
        return response.decode("utf-8")
    except Exception as e:
        return str(e)

def play_transition_then_load(channel):
    """
    Plays the transition video, then loads the scheduled video for the channel.
    """
    global current_channel
    current_channel = channel
    if os.path.exists(TRANSITION_VIDEO):
        print("DEBUG: Playing transition video (web).")
        transition_cmd = {"command": ["loadfile", TRANSITION_VIDEO, "replace"]}
        send_mpv_command(transition_cmd)
        time.sleep(TRANSITION_LENGTH)
    else:
        print("DEBUG: Transition video not found; skipping transition (web).")
    video, _ = compute_current_video_and_offset(channel)  # Ignore the computed offset
    if not video:
        print(f"DEBUG: Channel '{channel}' has no videos!")
        return
    
    # Get video duration and calculate random start position
    duration = get_video_duration(video)
    # Start somewhere in the first 80% of the video to ensure there's enough play time
    random_offset = random.uniform(0, duration * 0.8) if duration > 0 else 0
    
    print(f"DEBUG: Loading channel {channel} video: {video} @ random offset {random_offset:.2f}s")
    load_cmd = {"command": ["loadfile", video, "replace"]}
    send_mpv_command(load_cmd)
    time.sleep(0.3)
    seek_cmd = {"command": ["set_property", "time-pos", random_offset]}
    send_mpv_command(seek_cmd)

def play_transition_then_next():
    """
    Plays the transition video, then randomly selects a video from the current channel.
    The starting position is randomly chosen within the video.
    """
    global current_channel
    if current_channel is None:
        print("DEBUG: No channel active (web).")
        return
    playlist = channel_playlists.get(current_channel, [])
    if not playlist:
        print(f"DEBUG: Channel '{current_channel}' has no videos (web)!")
        return
    
    current_video, _ = compute_current_video_and_offset(current_channel)
    if len(playlist) > 1:
        possible_videos = [vid for vid in playlist if vid != current_video]
        chosen_video = random.choice(possible_videos) if possible_videos else current_video
    else:
        chosen_video = playlist[0]
    
    # Get video duration and calculate random start position
    duration = get_video_duration(chosen_video)
    # Start somewhere in the first 80% of the video to ensure there's enough play time
    random_offset = random.uniform(0, duration * 0.8) if duration > 0 else 0
    
    print(f"DEBUG: Randomly selected next video in channel {current_channel}: {chosen_video} with random offset {random_offset:.2f}s")
    if os.path.exists(TRANSITION_VIDEO):
        print("DEBUG: Playing transition video (web) for next video.")
        transition_cmd = {"command": ["loadfile", TRANSITION_VIDEO, "replace"]}
        send_mpv_command(transition_cmd)
        time.sleep(TRANSITION_LENGTH)
    load_cmd = {"command": ["loadfile", chosen_video, "replace"]}
    send_mpv_command(load_cmd)
    time.sleep(0.3)
    seek_cmd = {"command": ["set_property", "time-pos", random_offset]}
    send_mpv_command(seek_cmd)

# ----------------------------------------------------------------
# AUTO MODE FUNCTIONALITY
# ----------------------------------------------------------------
# Global variable for auto mode: "global", "local", or None.
auto_mode = None

# Global auto interval in seconds (default 120 seconds)
auto_interval = 120

# Global channel queue for more even random selection.
global_channel_queue = []

def get_next_random_channel():
    global global_channel_queue
    if not global_channel_queue:
        global_channel_queue = list(channels.keys())
        random.shuffle(global_channel_queue)
    return global_channel_queue.pop(0)

def auto_mode_loop():
    """
    Loop that, if auto_mode is enabled, triggers an automatic video change every auto_interval seconds.
      - "global": switches to the next channel from a shuffled queue.
      - "local": switches to a random video within the current channel.
    Also displays a countdown timer in the debug output.
    """
    global auto_mode, auto_interval
    while True:
        if auto_mode is not None:
            countdown = auto_interval
            while countdown > 0 and auto_mode is not None:
                print(f"DEBUG: Auto mode ({auto_mode}) active; {countdown} seconds until next change.")
                time.sleep(1)
                countdown -= 1
            if auto_mode is None:
                continue
            if auto_mode == "global":
                random_channel = get_next_random_channel()
                print(f"DEBUG: Auto mode (global) switching to channel: {random_channel}")
                play_transition_then_load(random_channel)
            elif auto_mode == "local":
                if current_channel is None:
                    random_channel = get_next_random_channel()
                    print(f"DEBUG: Auto mode (local) no current channel; switching to {random_channel}")
                    play_transition_then_load(random_channel)
                else:
                    print("DEBUG: Auto mode (local) switching to next video in current channel.")
                    play_transition_then_next()
        else:
            time.sleep(1)

# ----------------------------------------------------------------
# FLASK ENDPOINTS
# ----------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", channels=list(channels.keys()), auto_interval=auto_interval)

@app.route("/switch_channel", methods=["POST"])
def web_switch_channel():
    data = request.get_json()
    channel = data.get("channel")
    if not channel:
        return jsonify({"error": "No channel provided"}), 400
    play_transition_then_load(channel)
    return jsonify({"status": "success", "channel": channel})

@app.route("/next_video", methods=["POST"])
def web_next_video():
    play_transition_then_next()
    return jsonify({"status": "success", "action": "next_video"})

@app.route("/set_auto_mode", methods=["POST"])
def set_auto_mode():
    """
    Sets the auto mode.
      - Accepts JSON payload with {"mode": "global"} or {"mode": "local"} or {"mode": "off"}.
      - Returns the new mode.
    """
    global auto_mode
    data = request.get_json()
    mode = data.get("mode")
    if mode in ["global", "local"]:
        auto_mode = mode
        print(f"DEBUG: Auto mode set to {mode}")
        return jsonify({"mode": mode})
    elif mode == "off":
        auto_mode = None
        print("DEBUG: Auto mode turned off")
        return jsonify({"mode": "off"})
    else:
        return jsonify({"error": "Invalid mode"}), 400

@app.route("/set_auto_interval", methods=["POST"])
def set_auto_interval():
    """
    Sets the auto interval (in seconds) for auto mode.
    Accepts JSON payload with {"interval": <seconds>}.
    """
    global auto_interval
    data = request.get_json()
    try:
        interval = int(data.get("interval"))
        if interval <= 0:
            raise ValueError
        auto_interval = interval
        print(f"DEBUG: Auto interval set to {auto_interval} seconds")
        return jsonify({"interval": auto_interval})
    except:
        return jsonify({"error": "Invalid interval"}), 400

# ----------------------------------------------------------------
# RUN THE FLASK SERVER
# ----------------------------------------------------------------
if __name__ == "__main__":
    t_auto = threading.Thread(target=auto_mode_loop, daemon=True)
    t_auto.start()
    app.run(host="0.0.0.0", port=5000, debug=True)
