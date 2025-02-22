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

# Optional: if you want a route for the homepage
@app.route("/")
def home():
    # Ensure you have templates/index.html for rendering
    return render_template("index.html")

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
    - No low-pass by default
    - Normalize => mild compression effect
    - +5 dB gain
    - Force ~-12 dBFS for loudness
    """
    try:
        audio = AudioSegment.from_wav(input_wav)
        # Minimal high-pass
        audio = audio.high_pass_filter(40)

        # Normalize => mild compression
        audio = effects.normalize(audio)

        # +5 dB
        audio = audio.apply_gain(5)

        # -12 LUFS
        target_lufs = -12.0
        gain_needed = target_lufs - audio.dBFS
        audio = audio.apply_gain(gain_needed)

        audio.export(output_wav, format="wav")
        return True
    except Exception as e:
        print(f"[final_mastering_chain] Error: {e}")
        return False

@app.route("/upload", methods=["POST"])
def upload_file():
    # Check if file is in request
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    user_file = request.files["file"]
    mix_type = request.form.get("mix_type", "StudioMaster")

    # 1) Save user file
    user_upload_path = os.path.join(UPLOAD_FOLDER, user_file.filename)
    user_file.save(user_upload_path)

    # Original filename stem
    original_stem = os.path.splitext(user_file.filename)[0]

    # 2) Convert user => WAV
    user_wav = os.path.join(PROCESSED_FOLDER, "user_input.wav")
    if not convert_audio_to_wav_pydub(user_upload_path, user_wav):
        return jsonify({"error": "Failed to convert user file to WAV"}), 500

    # 3) Attempt to find & convert reference
    possible_exts = [".mp3", ".wav", ".flac", ".m4a"]
    ref_wav = os.path.join(PROCESSED_FOLDER, "reference.wav")
    found_ref = False

    for ext in possible_exts:
        ref_original = os.path.join(REFERENCE_FOLDER, mix_type + ext)
        if os.path.exists(ref_original):
            print(f"[DEBUG] Found reference for {mix_type}: {ref_original}")
            if convert_audio_to_wav_pydub(ref_original, ref_wav):
                found_ref = True
            break

    # AI intermediate file
    ai_mastered_path = os.path.join(PROCESSED_FOLDER, "ai_mastered.wav")
    matchering_succeeded = False

    # Debug info
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
                print("[DEBUG] Attempting mg.process(...)")
                mg.process(user_wav, ref_wav, ai_mastered_path)
                print("[upload_file] AI match success!")
                matchering_succeeded = True
            except Exception as e:
                print(f"[upload_file] Matchering error: {e}")
    else:
        print(f"[DEBUG] No reference found => skipping AI match")

    # 5) Fallback if AI fails
    if not matchering_succeeded:
        try:
            AudioSegment.from_wav(user_wav).export(ai_mastered_path, format="wav")
            print("[DEBUG] Fallback: Copied user_wav to ai_mastered.wav")
        except Exception as e:
            return jsonify({"error": f"Fallback copy error: {e}"}), 500

    # 6) Final mastering => <stem>_master.wav
    final_filename = f"{original_stem}_master.wav"
    final_path = os.path.join(PROCESSED_FOLDER, final_filename)

    if not final_mastering_chain(ai_mastered_path, final_path):
        return jsonify({"error": "Final mastering chain failed"}), 500

    return send_file(final_path, as_attachment=True)

# For local testing, pick a port from environment or default 5000
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Avoid debug=True in production; Heroku uses Gunicorn for final anyway
    app.run(host="0.0.0.0", port=port, debug=True)
