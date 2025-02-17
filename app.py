import os
import socket
import librosa
import numpy as np
import soundfile as sf
from pydub import AudioSegment, effects
import matchering as mg
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
PROCESSED_FOLDER = "processed"
REFERENCE_FOLDER = "reference_tracks"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)
os.makedirs(REFERENCE_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0

def convert_audio_to_wav_pydub(input_path, output_path):
    """
    Uses pydub + FFmpeg to convert any supported format (MP3, WAV, FLAC, etc.) 
    to a standard 16-bit 44.1kHz WAV.
    """
    try:
        audio = AudioSegment.from_file(input_path)
        audio = audio.set_frame_rate(44100).set_channels(2).set_sample_width(2)  # 16-bit
        audio.export(output_path, format="wav")
        return True
    except Exception as e:
        print(f"[convert_audio_to_wav_pydub] Error converting {input_path} -> {output_path}: {e}")
        return False

def final_mastering_chain(input_wav, output_wav):
    """
    Fallback or final step:
    - High-pass ~40Hz to remove sub-rumble
    - No low-pass to avoid muffling
    - Normalize => mild compression
    - +5 dB gain
    - Target ~-12 dBFS for extra loudness
    """
    try:
        audio = AudioSegment.from_wav(input_wav)

        # Minimal high-pass ~40Hz
        audio = audio.high_pass_filter(40)

        # If you'd like a mild low-pass, you could do:
        # audio = audio.low_pass_filter(19000)

        # Normalize => mild compression effect
        audio = effects.normalize(audio)

        # +5 dB
        audio = audio.apply_gain(5)

        # Force ~-12 dBFS
        target_lufs = -12.0
        current_lufs = audio.dBFS
        gain_needed = target_lufs - current_lufs
        audio = audio.apply_gain(gain_needed)

        audio.export(output_wav, format="wav")
        print(f"[final_mastering_chain] Done => {output_wav}")
        return True
    except Exception as e:
        print(f"[final_mastering_chain] Error: {e}")
        return False

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    user_file = request.files["file"]
    mix_type = request.form.get("mix_type", "StudioMaster")

    # 1) Save user file
    user_upload_path = os.path.join(UPLOAD_FOLDER, user_file.filename)
    user_file.save(user_upload_path)

    # 2) Convert user => WAV
    user_wav = os.path.join(PROCESSED_FOLDER, "user_input.wav")
    if not convert_audio_to_wav_pydub(user_upload_path, user_wav):
        return jsonify({"error": "Failed to convert user file to WAV"}), 500

    # 3) Attempt to find & convert reference track
    possible_exts = [".mp3", ".wav", ".flac", ".m4a"]
    ref_wav = os.path.join(PROCESSED_FOLDER, "reference.wav")
    found_ref = False

    for ext in possible_exts:
        ref_original = os.path.join(REFERENCE_FOLDER, mix_type + ext)
        if os.path.exists(ref_original):
            print(f"[DEBUG] Found reference file for {mix_type}: {ref_original}")
            if convert_audio_to_wav_pydub(ref_original, ref_wav):
                found_ref = True
            break

    ai_mastered_path = os.path.join(PROCESSED_FOLDER, "ai_mastered.wav")
    matchering_succeeded = False

    # Additional debug: print file sizes/frames
    def debug_file_stats(path, label):
        try:
            stat = os.stat(path)
            print(f"[DEBUG] {label} => size: {stat.st_size} bytes")
            with sf.SoundFile(path) as sf_file:
                print(f"         frames: {sf_file.frames}, samplerate: {sf_file.samplerate}")
        except Exception as e:
            print(f"[DEBUG] Could not read {label}: {e}")

    # 4) If reference found, attempt AI matching
    if found_ref:
        debug_file_stats(user_wav, "user_wav")
        debug_file_stats(ref_wav, "ref_wav")

        if os.path.samefile(user_wav, ref_wav):
            print("[DEBUG] user_wav == ref_wav => skipping AI match.")
        else:
            try:
                print(f"[DEBUG] Attempting mg.process(user_wav={user_wav}, ref_wav={ref_wav})...")
                mg.process(user_wav, ref_wav, ai_mastered_path)
                print("[upload_file] mg.process => success!")
                matchering_succeeded = True
            except Exception as e:
                print(f"[upload_file] Matchering error: {e}")
    else:
        print(f"[DEBUG] No reference found for {mix_type} => skipping AI match")

    # 5) Fallback => copy user_wav -> ai_mastered_path if AI failed
    if not matchering_succeeded:
        try:
            AudioSegment.from_wav(user_wav).export(ai_mastered_path, format="wav")
            print("[DEBUG] Fallback: Copied user_wav to ai_mastered.wav")
        except Exception as e:
            print(f"[upload_file] Fallback copy error: {e}")
            return jsonify({"error": "Fallback copy error"}), 500

    # 6) Final mastering chain
    final_path = os.path.join(PROCESSED_FOLDER, f"final_{mix_type}.wav")
    if not final_mastering_chain(ai_mastered_path, final_path):
        return jsonify({"error": "Final mastering chain failed"}), 500

    return send_file(final_path, as_attachment=True)

port = 8080 if not is_port_in_use(8080) else 5001

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=port)
