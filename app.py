from flask import Flask, request, jsonify, send_file, render_template
import os
import socket
import librosa
import soundfile as sf
import numpy as np
import matchering as mg
from pydub import AudioSegment, effects

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

def convert_to_wav(input_path, output_path):
    """
    Convert any audio file (MP3, WAV, FLAC, etc.) to WAV using librosa & soundfile.
    """
    try:
        y, sr = librosa.load(input_path, sr=None, mono=True)
        sf.write(output_path, y, sr)
        return True
    except Exception as e:
        print(f"[Error] Converting {input_path} to WAV: {e}")
        return False

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    mix_type = request.form.get("mix_type", "StudioMaster")  # Default style

    file_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(file_path)

    # 1) Convert user file -> WAV
    user_wav = os.path.join(PROCESSED_FOLDER, "user.wav")
    if not convert_to_wav(file_path, user_wav):
        return jsonify({"error": "Failed to convert user file to WAV"}), 500

    # 2) Convert reference -> WAV (if it exists), else fallback to user
    ref_original = os.path.join(REFERENCE_FOLDER, f"{mix_type}.wav")
    if os.path.exists(ref_original):
        ref_wav = os.path.join(PROCESSED_FOLDER, "reference.wav")
        if not convert_to_wav(ref_original, ref_wav):
            return jsonify({"error": "Failed to convert reference to WAV"}), 500
    else:
        # Use user's track if there's no dedicated reference
        ref_wav = user_wav

    # 3) AI Mix & Master
    master_wav = os.path.join(PROCESSED_FOLDER, "ai_mastered.wav")
    try:
        mg.process(
            user_wav,  # target
            ref_wav,   # reference
            master_wav # output
        )
    except Exception as e:
        print(f"[Error] Matchering process: {e}")
        return jsonify({"error": "Matchering failed"}), 500

    # 4) Final polish with pydub
    try:
        audio = AudioSegment.from_wav(master_wav)
        # Light compression
        audio = effects.normalize(audio)
        # Subtle volume bump
        audio = audio.apply_gain(2)
        # Ensure around -14 dBFS
        gain_needed = -14.0 - audio.dBFS
        audio = audio.apply_gain(gain_needed)

        # Export final
        final_path = os.path.join(PROCESSED_FOLDER, f"final_{mix_type}.wav")
        audio.export(final_path, format="wav")
    except Exception as e:
        print(f"[Error] Final polishing: {e}")
        return jsonify({"error": "Final polishing failed"}), 500

    return send_file(final_path, as_attachment=True)

# Choose port
port = 8080 if not is_port_in_use(8080) else 5001

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=port)
