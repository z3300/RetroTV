#!/usr/bin/env python3
import sys, os, time, threading, glob, subprocess, datetime, random, json, socket
from concurrent.futures import ThreadPoolExecutor

# -----------------------------------------------------------------------------
# Raspberry Pi Version:
#  - BASE_PATHS include the current working directory and "/mnt/ssd/compressed_vid".
#  - Channels are auto-detected from each base path: only subdirectories whose names include "channel"
#    (case-insensitive) are used.
#  - Video durations are calculated using ffprobe with caching and parallel processing.
#  - mpv is launched with --hwdec=mmal for hardware decoding.
#  - New auto mode: user can type "auto global" to change to a random channel every 2 minutes,
#    or "auto shuffle" to shuffle videos within the current channel every 2 minutes.
# -----------------------------------------------------------------------------

# Define base paths to search for channel folders.
BASE_PATHS = [os.getcwd(), "/mnt/myhdd/compressed_vid"]

# Auto-detect channels: scan each base path and include subdirectories with "channel" in the name.
channels = {}  # mapping from channel name to its full path
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

# Cache file for durations.
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

# Global cache loaded at startup.
durations_cache = load_cache()

def get_video_duration(video_path):
    """
    Uses ffprobe to get the duration (in seconds) of a video.
    """
    try:
        cmd = [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        duration = float(result.stdout.strip())
        print(f"DEBUG: Duration for {video_path}: {duration:.2f} seconds.")
        return duration
    except Exception as e:
        print(f"DEBUG: Could not obtain duration for {video_path}: {e}")
        return 0

def cached_get_video_duration(video_path):
    """
    Checks if the duration of video_path is cached (based on modification time).
    If not, calculates it and updates the cache.
    """
    mod_time = os.path.getmtime(video_path)
    key = video_path
    if key in durations_cache and durations_cache[key]["mod_time"] == mod_time:
        return durations_cache[key]["duration"]
    else:
        duration = get_video_duration(video_path)
        durations_cache[key] = {"mod_time": mod_time, "duration": duration}
        return duration

# Dictionaries for playlists and durations.
channel_playlists = {}  # channel name -> list of video file paths
channel_durations = {}  # channel name -> list of corresponding durations

def init_playlists():
    """
    Scans each channel folder and builds playlists.
    Uses ThreadPoolExecutor for parallel processing.
    """
    for ch, folder_path in channels.items():
        files = sorted(glob.glob(os.path.join(folder_path, "*.*")))
        if files:
            random.shuffle(files)
        channel_playlists[ch] = files
        with ThreadPoolExecutor(max_workers=8) as executor:
            durations = list(executor.map(cached_get_video_duration, files))
        channel_durations[ch] = durations
        total = sum(durations)
        print(f"DEBUG: Total duration for channel '{ch}': {total:.2f} seconds.")
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
        print(f"Warning: Total duration for channel '{channel}' is 0. Using first video with offset 0.")
        return playlist[0], 0
    elapsed = get_seconds_since_midnight()
    channel_pos = elapsed % total_duration
    cumulative = 0
    for video, d in zip(playlist, durations):
        if cumulative + d > channel_pos:
            offset = channel_pos - cumulative
            print(f"DEBUG: For channel '{channel}', elapsed {elapsed:.2f}s, channel_pos {channel_pos:.2f}s, video {video}, offset {offset:.2f}s")
            return video, offset
        cumulative += d
    return playlist[-1], 0

# --- Start a Persistent mpv Instance with IPC ---
IPC_SOCKET = "/tmp/mpv-socket"
if os.path.exists(IPC_SOCKET):
    os.remove(IPC_SOCKET)

def start_mpv():
    """
    Launches mpv with an IPC socket.
    On the Pi we use hardware decoding (--hwdec=mmal).
    Output is redirected so that terminal input remains available.
    """
    default_channel = list(channels.keys())[0]
    video, offset = compute_current_video_and_offset(default_channel)
    if video is None:
        print("No video found to start mpv.")
        sys.exit(1)
    cmd = [
        "mpv", "--loop", "--hwdec=mmal", "--no-input-default-bindings",
        "--input-ipc-server=" + IPC_SOCKET, "--quiet",
        f"--start={offset}", video
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    timeout = 5  # seconds
    start_time = time.time()
    while not os.path.exists(IPC_SOCKET):
        if time.time() - start_time > timeout:
            print("ERROR: IPC socket was not created by mpv in time.")
            break
        time.sleep(0.1)
    return proc

mpv_process = start_mpv()

def send_mpv_command(command):
    """
    Sends a JSON command to mpv via the IPC socket.
    """
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(IPC_SOCKET)
        sock.sendall((json.dumps(command) + "\n").encode("utf-8"))
        response = sock.recv(1024)
        sock.close()
        return response
    except Exception as e:
        print("DEBUG: Error sending command to mpv:", e)

def switch_channel(channel, use_random_offset=False):
    """
    Switches to the specified channel with a transition.
    Plays the transition video then loads the scheduled video at its computed offset.
    If use_random_offset is True, a random offset is used instead of the scheduled offset.
    """
    global current_channel
    current_channel = channel
    if os.path.exists(TRANSITION_VIDEO):
        print("DEBUG: Playing transition video.")
        transition_command = {"command": ["loadfile", TRANSITION_VIDEO, "replace"]}
        send_mpv_command(transition_command)
        time.sleep(TRANSITION_LENGTH)
    else:
        print("DEBUG: Transition video not found; skipping transition.")
    video, scheduled_offset = compute_current_video_and_offset(channel)
    if video is None:
        print(f"DEBUG: Channel '{channel}' has no videos!")
        return
    if use_random_offset:
        duration = get_video_duration(video)
        offset = random.uniform(0, duration * 0.8) if duration > 0 else 0
        print(f"DEBUG: Switching to channel {channel}: video {video}, using random offset {offset:.2f} seconds.")
    else:
        offset = scheduled_offset
        print(f"DEBUG: Switching to channel {channel}: video {video}, using scheduled offset {offset:.2f} seconds.")
    load_command = {"command": ["loadfile", video, "replace"]}
    send_mpv_command(load_command)
    time.sleep(0.3)
    seek_command = {"command": ["set_property", "time-pos", offset]}
    send_mpv_command(seek_command)

def next_video(use_random_offset=False):
    """
    Switches to a random video in the current channel.
    If use_random_offset is True, a random start offset is chosen.
    Otherwise, the offset is computed based on current time.
    """
    global current_channel
    if current_channel is None:
        print("DEBUG: No channel is currently active.")
        return
    playlist = channel_playlists.get(current_channel, [])
    if not playlist:
        print(f"DEBUG: Channel '{current_channel}' has no videos!")
        return
    chosen_video = random.choice(playlist)
    duration = get_video_duration(chosen_video)
    if use_random_offset:
        offset = random.uniform(0, duration * 0.8) if duration > 0 else 0
        print(f"DEBUG: Randomly selected next video in channel {current_channel}: {chosen_video} with random offset {offset:.2f}s")
    else:
        offset = (get_seconds_since_midnight() % duration) if duration > 0 else 0
        print(f"DEBUG: Next video in channel {current_channel}: {chosen_video} with scheduled offset {offset:.2f}s")
    if os.path.exists(TRANSITION_VIDEO):
        print("DEBUG: Playing transition video for next video.")
        transition_command = {"command": ["loadfile", TRANSITION_VIDEO, "replace"]}
        send_mpv_command(transition_command)
        time.sleep(TRANSITION_LENGTH)
    load_command = {"command": ["loadfile", chosen_video, "replace"]}
    send_mpv_command(load_command)
    time.sleep(0.3)
    seek_command = {"command": ["set_property", "time-pos", offset]}
    send_mpv_command(seek_command)

# ----------------------------------------------------------------
# AUTO MODE FUNCTIONALITY
# ----------------------------------------------------------------
# Global variable for auto mode: "global", "shuffle", or None.
auto_mode = None

# For better global randomness, maintain a shuffled queue.
global_channel_queue = []

def get_next_random_channel():
    global global_channel_queue
    if not global_channel_queue:
        global_channel_queue = list(channels.keys())
        random.shuffle(global_channel_queue)
    return global_channel_queue.pop(0)

def auto_mode_loop():
    """
    Loop that, if auto_mode is enabled, triggers an automatic video change every 2 minutes.
      - "global": switches to the next channel from a shuffled queue.
      - "shuffle": switches to a random video within the current channel.
    Also displays a countdown timer in the debug output.
    """
    global auto_mode
    while True:
        if auto_mode is not None:
            countdown = 120  # 2 minutes in seconds
            while countdown > 0 and auto_mode is not None:
                print(f"DEBUG: Auto mode ({auto_mode}) active; {countdown} seconds until next change.")
                time.sleep(1)
                countdown -= 1
            if auto_mode is None:
                continue
            if auto_mode == "global":
                random_channel = get_next_random_channel()
                print(f"DEBUG: Auto mode (global) switching to channel: {random_channel}")
                switch_channel(random_channel, use_random_offset=True)
            elif auto_mode == "shuffle":
                if current_channel is None:
                    random_channel = get_next_random_channel()
                    print(f"DEBUG: Auto mode (shuffle) no current channel; switching to {random_channel}")
                    switch_channel(random_channel, use_random_offset=True)
                else:
                    print("DEBUG: Auto mode (shuffle) switching to next video in current channel.")
                    next_video(use_random_offset=True)
        else:
            time.sleep(1)

def terminal_input_thread():
    """
    Listens for terminal input to switch channels, videos, or control auto mode.
    Accepts:
      - A valid channel name to switch channels.
      - "next" for next video.
      - "auto global" or "auto shuffle" to enable auto mode.
      - "auto off" to disable auto mode.
      - "q" to quit.
    """
    global auto_mode
    while True:
        try:
            cmd_input = input("Enter channel name, 'next', 'auto global', 'auto shuffle', 'auto off', or 'q' to quit: ").strip()
        except EOFError:
            continue
        lower = cmd_input.lower()
        if lower == 'q':
            print("Exiting...")
            mpv_process.terminate()
            mpv_process.wait()
            os._exit(0)
        elif lower == 'next':
            next_video(use_random_offset=True)
        elif lower.startswith("auto"):
            tokens = lower.split()
            if len(tokens) == 2 and tokens[1] in ["global", "shuffle"]:
                auto_mode = tokens[1]
                print(f"DEBUG: Auto mode set to {auto_mode}.")
            elif len(tokens) == 2 and tokens[1] == "off":
                auto_mode = None
                print("DEBUG: Auto mode turned off.")
            else:
                print("Unknown auto mode command. Use 'auto global', 'auto shuffle', or 'auto off'.")
        elif cmd_input in channels:
            switch_channel(cmd_input, use_random_offset=True)
        else:
            print("Unknown command. Available channels:", ", ".join(channels.keys()), "or 'next', or auto commands.")

if __name__ == "__main__":
    print("Available channels:", ", ".join(channels.keys()))
    # Start terminal input thread.
    t = threading.Thread(target=terminal_input_thread, daemon=True)
    t.start()
    # Start auto mode loop thread.
    t_auto = threading.Thread(target=auto_mode_loop, daemon=True)
    t_auto.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        mpv_process.terminate()
        mpv_process.wait()
        sys.exit(0)
