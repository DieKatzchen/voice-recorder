#!/usr/bin/env python3
"""
Simple tkinter application for recording voice samples with text prompts.

Reads prompts from a text file in the CMU Arctic format.
Records 48Khz, 16-bit, stereo WAV files using "arecord".
Writes WAV files to a directory with a timestamp appended to the prompt id.
"""
import argparse
import logging
import re
import shlex
import subprocess
import threading
import time
import tkinter as tk
import tkinter.messagebox
import typing
import wave
from pathlib import Path
from tkinter import filedialog, ttk

# Directory of this script
_DIR = Path(__file__).parent

# Regex for prompt lines
_ARCTIC_LINE = re.compile(r'^\s*\(\s*([^ ]+)\s+"([^"]+)"\s*\)\s*$')

# Shared logger
_LOGGER = logging.getLogger("record")

# True if currectly recording
_IS_RECORDING = False

_RECORDING_DONE = threading.Event()

_RECORDING_PATH = None

_SAMPLE_RATE = 48000  # Hertz
_SAMPLE_WIDTH = 2  # bytes
_SAMPLE_CHANNELS = 2

# -----------------------------------------------------------------------------


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", help="Name of ALSA recording device")
    parser.add_argument("--prompts", help="File with prompts (CMU Arctic format)")
    parser.add_argument("--wav", help="Directory to store WAV files")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=2048,
        help="Bytes per chunk to read from microphone",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG)

    window = tk.Tk()
    window.title("Voice Recorder")

    if args.prompts is None:
        # Get prompts file from dialog box
        args.prompts = filedialog.askopenfilename(
            master=window, title="Select prompts file", filetypes=[("All files", "*")]
        )

    assert args.prompts, "No prompts file"

    if args.wav is None:
        # Get WAV directory from dialog box
        args.wav = filedialog.askdirectory(
            master=window, title="Select WAV directory", mustexist=False
        )

    assert args.wav, "No WAV directory"

    wav_dir = Path(args.wav)
    wav_dir.mkdir(parents=True, exist_ok=True)

    _LOGGER.debug(args)

    # -------------------------------------------------------------------------
    # Read Prompts
    # -------------------------------------------------------------------------

    _LOGGER.debug("Loading prompts from %s", args.prompts)

    # id -> text
    prompts: typing.Dict[str, str] = {}
    with open(args.prompts, "r") as prompts_file:
        for line in prompts_file:
            line = line.strip()
            if line:
                # ( prompt_id "Text for prompt" )
                match = _ARCTIC_LINE.match(line)
                if match:
                    prompt_id = match.group(1)
                    prompt_text = match.group(2)
                    prompts[prompt_id] = prompt_text

    assert prompts, "No prompts!"
    _LOGGER.debug("Loaded %s prompt(s)", len(prompts))

    # Remaining prompt ids
    prompts_left: typing.List[str] = list(prompts.keys())

    # Total number of prompts
    total_prompts: int = len(prompts_left)

    # Current prompt id or None
    current_prompt_id: typing.Optional[str] = None

    # Last recorded WAV path or None
    last_wav_path: typing.Optional[Path] = None

    # -------------------------------------------------------------------------
    # Record Samples
    # -------------------------------------------------------------------------

    # Start recording thread
    record_thread = threading.Thread(target=recording_proc, daemon=True, args=(args,))
    record_thread.start()

    # Text box with prompt text
    textbox = tk.Text(window, height=10, wrap=tk.WORD)
    textbox.config(font=("Courier", 20))
    textbox.pack(fill=tk.BOTH, padx=10, pady=10)

    # Progress bar for how many prompts are done
    progress = ttk.Progressbar(
        window, orient=tk.HORIZONTAL, length=100, mode="determinate"
    )
    progress.pack(fill="x", padx=10, pady=10)

    record_button = None
    play_button = None
    next_button = None

    def do_next(*_args):
        """Get the next prompt and show text."""
        nonlocal current_prompt_id, last_wav_path
        current_prompt_id = None
        last_wav_path = None

        next_button.config(bg="#F0F0F0")
        record_button.config(bg="yellow")

        if prompts_left:
            # Find first unfinished prompts
            wav_names = [wav_path.name for wav_path in wav_dir.glob("*.wav")]
            current_prompt_id = prompts_left.pop()
            wav_prefix = f"{current_prompt_id}_"
            while has_wav(wav_names, wav_prefix):
                current_prompt_id = None
                if prompts_left:
                    current_prompt_id = prompts_left.pop()
                    wav_prefix = f"{current_prompt_id}_"
                else:
                    break

        if current_prompt_id:
            # Show prompt text
            textbox.delete(1.0, tk.END)
            textbox.insert(1.0, prompts[current_prompt_id])
            next_button["state"] = tk.DISABLED
        else:
            tkinter.messagebox.showinfo(message="All done :)")

        # Update progress bar and next count
        progress["value"] = 100 * ((total_prompts - len(prompts_left)) / total_prompts)
        next_button["text"] = f"NEXT ({len(prompts_left)})"

    def do_play(*_args):
        """Play last recorded WAV file"""
        nonlocal last_wav_path

        print(last_wav_path)
        if last_wav_path and last_wav_path.is_file():
            threading.Thread(
                target=lambda: subprocess.check_call(
                    ["aplay", "-q", str(last_wav_path)]
                )
            ).start()

    def do_record(*_args):
        """Toggle recording."""
        global _IS_RECORDING, _RECORDING_DONE, _RECORDING_PATH
        nonlocal last_wav_path

        if _IS_RECORDING:
            # Stop recording and wait for signal
            _IS_RECORDING = False

            _LOGGER.debug("Waiting for recording to end")
            _RECORDING_DONE.wait()

            window.config(background="#F0F0F0")
            record_button.config(bg="green", activebackground="red")
            record_button["text"] = "RECORD"
            play_button["state"] = tk.NORMAL
            next_button["state"] = tk.NORMAL
            next_button.config(bg="yellow")

            if current_prompt_id and last_wav_path:
                # Write prompt text to file
                text_path = last_wav_path.with_suffix(".txt")
                text_path.write_text(prompts[current_prompt_id])
        else:
            # Start recording
            if current_prompt_id:
                window.config(background="red")
                record_button.config(activebackground="white")
                record_button["text"] = "FINISH"
                play_button["state"] = tk.DISABLED
                next_button.config(bg="#F0F0F0")

                last_wav_path = (
                    wav_dir / f"{current_prompt_id}_{time.time()}"
                ).with_suffix(".wav")

                # Signal other thread
                _RECORDING_PATH = last_wav_path
                _RECORDING_DONE.clear()
                _IS_RECORDING = True

            else:
                tkinter.messagebox.showinfo(message="No prompt")

    # -------------------------------------------------------------------------

    bottom_frame = tk.Frame(window)
    bottom_frame.pack(fill=tk.BOTH, padx=10, pady=10)

    # Start/stop recording button
    record_button = tk.Button(bottom_frame, text="RECORD", command=do_record)
    record_button.config(bg="yellow", activebackground="red", font=("Courier", 20))
    record_button.pack(side="left", padx=10, pady=10)

    # Next prompt button
    next_button = tk.Button(bottom_frame, text="Next", command=do_next)
    next_button.config(font=("Courier", 20))
    next_button.pack(side="right", padx=10, pady=10)

    # Play back last recording button
    play_button = tk.Button(bottom_frame, text="Play", command=do_play)
    play_button.config(font=("Courier", 20))
    play_button.pack(side="right", padx=10, pady=10)

    do_next()

    window.mainloop()


# -----------------------------------------------------------------------------


def recording_proc(args: argparse.Namespace):
    """Drops audio chunks until recording"""
    global _IS_RECORDING, _RECORDING_PATH, _RECORDING_DONE
    try:
        arecord_cmd = [
            "arecord",
            "-q",
            "-r",
            str(_SAMPLE_RATE),
            "-c",
            str(_SAMPLE_CHANNELS),
            "-f",
            "S16_LE",
        ]

        if args.device:
            arecord_cmd.extend(["-D", shlex.quote(args.device)])

        _LOGGER.debug(arecord_cmd)

        # Start recording process
        proc = subprocess.Popen(arecord_cmd, stdout=subprocess.PIPE)
        assert proc.stdout, "No stdout"
        record_wave_file = None

        while True:
            chunk = proc.stdout.read(args.chunk_size)
            if _IS_RECORDING:
                if record_wave_file is None:
                    # Start recording to a WAV file
                    assert _RECORDING_PATH, "No recording path"
                    _LOGGER.debug("Recording to %s", _RECORDING_PATH)

                    record_wave_file = wave.open(str(_RECORDING_PATH), "w")
                    record_wave_file.setframerate(_SAMPLE_RATE)
                    record_wave_file.setsampwidth(_SAMPLE_WIDTH)
                    record_wave_file.setnchannels(_SAMPLE_CHANNELS)

                record_wave_file.writeframes(chunk)
            elif record_wave_file:
                # Close WAV file and signal completion
                record_wave_file.close()
                record_wave_file = None
                _RECORDING_DONE.set()
    except Exception:
        _LOGGER.exception("recording_proc")


# -----------------------------------------------------------------------------


def has_wav(wav_names, wav_prefix):
    """True if WAV file with prefix already exists."""
    for wav_name in wav_names:
        if wav_name.startswith(wav_prefix):
            return True

    return False


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    main()
