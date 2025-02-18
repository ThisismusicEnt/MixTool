import os
import socket
import librosa
import numpy as np
import soundfile as sf
from pydub import AudioSegment, effects
import matchering as mg
from flask import Flask, request, jsonify, send_file, render_template, send_from_directory
from flask_cors import CORS
import subprocess
import logging

app = Flask(__name__)
CORS(app)  # Enable CORS for frontend integration

UPLOAD_FOLDER = "uploads"
PROCESSED_FOLDER = "processed"
REFERENCE_FOLDER = "reference_tracks"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)
os.makedirs(REFERENCE_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Ensure ffmpeg is installed
FFMPEG_PATH = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True).stdout.strip()
if not FFMPEG_PATH:
    logging.warning("ffmpeg is not installed. Audio processing may fail!")

# Serve static files
@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(app.static_folder, filename)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(app.static_folder, 'favicon.ico')

# Optional: if you want a route for the homepage
@app.route("/")
def home():
    return render_template("index.html")

def convert_audio_to_wav_pydub(input_path, output_path):
    try:
        audio = AudioSegment.from_file(input_path)
        audio = audio.set_frame_rate(44100).set_channels(2).set_sample_width(2)  # 16-bit
        audio.export(output_path, format="wav")
        return True
    except Exception as e:
        print(f"[convert_audio_to_wav_pydub] Error converting {input_path} -> {output_path}: {e}")
        return False

def final_mastering_chain(input_wav, output_wav):
    try:
        audio = AudioSegment.from_wav(input_wav)
        audio = audio.high_pass_filter(40)
        audio = effects.normalize(audio)
        audio = audio.apply_gain(5)
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
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    user_file = request.files["file"]
    mix_type = request.form.get("mix_type", "StudioMaster")

    user_upload_path = os.path.join(UPLOAD_FOLDER, user_file.filename)
    user_file.save(user_upload_path)

    original_stem = os.path.splitext(user_file.filename)[0]

    user_wav = os.path.join(PROCESSED_FOLDER, "user_input.wav")
    if not convert_audio_to_wav_pydub(user_upload_path, user_wav):
        return jsonify({"error": "Failed to convert user file to WAV"}), 500

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

    ai_mastered_path = os.path.join(PROCESSED_FOLDER, "ai_mastered.wav")
    matchering_succeeded = False

    def debug_file_stats(path, label):
        try:
            stat = os.stat(path)
            print(f"[DEBUG] {label} => size: {stat.st_size} bytes")
            with sf.SoundFile(path) as sf_file:
                print(f"         frames: {sf_file.frames}, samplerate: {sf_file.samplerate}")
        except Exception as e:
            print(f"[DEBUG] Could not read {label}: {e}")

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

    if not matchering_succeeded:
        try:
            AudioSegment.from_wav(user_wav).export(ai_mastered_path, format="wav")
            print("[DEBUG] Fallback: Copied user_wav to ai_mastered.wav")
        except Exception as e:
            return jsonify({"error": f"Fallback copy error: {e}"}), 500

    final_filename = f"{original_stem}_master.wav"
    final_path = os.path.join(PROCESSED_FOLDER, final_filename)

    if not final_mastering_chain(ai_mastered_path, final_path):
        return jsonify({"error": "Final mastering chain failed"}), 500

    return send_file(final_path, as_attachment=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)