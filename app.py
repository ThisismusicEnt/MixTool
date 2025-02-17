from flask import Flask, request, jsonify, send_file, render_template
import os
import socket
import librosa
import numpy as np
import soundfile as sf
from pydub import AudioSegment, effects
import matchering as mg

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

# ----------------------------------------------------------
# 1) HELPER: Convert MP3 or other format to WAV with pydub
# ----------------------------------------------------------
def convert_audio_to_wav_pydub(input_path, output_path):
    """
    Uses pydub + FFmpeg to convert any supported format (MP3, M4A, WAV) to a standard 16-bit 44.1kHz WAV.
    Requires FFmpeg installed on your system.
    """
    try:
        audio = AudioSegment.from_file(input_path)  # pydub auto-detects format
        # Standardize: 44.1kHz, stereo, 16-bit
        audio = audio.set_frame_rate(44100)
        audio = audio.set_channels(2)
        audio = audio.set_sample_width(2)  # 16-bit
        audio.export(output_path, format="wav")
        return True
    except Exception as e:
        print(f"[convert_audio_to_wav_pydub] Error: {e}")
        return False

# ----------------------------------------------------------
# 2) HELPER: Final Mastering Step (EQ + Compression + Loudness)
# ----------------------------------------------------------
def final_mastering_chain(input_wav, output_wav):
    """
    A fallback or final step that:
    - Removes sub-rumble & extreme highs (mild EQ)
    - Normalizes peaks (light compression effect)
    - Slight volume bump
    - Targets ~-14 dBFS loudness
    """
    try:
        audio = AudioSegment.from_wav(input_wav)

        # Basic EQ: Remove sub-rumble <50Hz, reduce extremes above 18kHz
        audio = audio.high_pass_filter(50)
        audio = audio.low_pass_filter(18000)

        # Light compression effect by normalizing peaks
        audio = effects.normalize(audio)

        # Small gain bump
        audio = audio.apply_gain(2)

        # Loudness to ~-14 dBFS
        target_lufs = -14.0
        current_lufs = audio.dBFS
        gain_needed = target_lufs - current_lufs
        audio = audio.apply_gain(gain_needed)

        audio.export(output_wav, format="wav")
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

    file = request.files["file"]
    mix_type = request.form.get("mix_type", "StudioMaster")  # e.g. "Pop", "Trap", "Lo-Fi", etc.

    # 1) Save user file to /uploads
    user_upload_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(user_upload_path)

    # 2) Convert user file => user_wav (true WAV)
    user_wav = os.path.join(PROCESSED_FOLDER, "user_input.wav")
    if not convert_audio_to_wav_pydub(user_upload_path, user_wav):
        return jsonify({"error": "Failed to convert user file to WAV"}), 500

    # 3) Convert reference if it exists => ref_wav
    #    We look for reference_tracks/<mix_type>.mp3 OR .wav
    #    If neither found, we skip AI matching
    found_ref = False
    possible_exts = [".mp3", ".wav", ".flac", ".m4a"]
    ref_wav = os.path.join(PROCESSED_FOLDER, "reference.wav")

    for ext in possible_exts:
        ref_orig = os.path.join(REFERENCE_FOLDER, mix_type + ext)
        if os.path.exists(ref_orig):
            print(f"[upload_file] Found reference {ref_orig}, converting to {ref_wav} ...")
            if convert_audio_to_wav_pydub(ref_orig, ref_wav):
                found_ref = True
                break

    ai_mastered_path = os.path.join(PROCESSED_FOLDER, "ai_mastered.wav")
    matchering_succeeded = False

    if found_ref:
        # 4) Attempt AI-based spectral matching
        # Ensure user_wav != ref_wav
        import os
        if os.path.samefile(user_wav, ref_wav):
            print("[upload_file] Reference is identical to user track. Skipping AI match.")
        else:
            print(f"[upload_file] Attempting mg.process with:\n  user_wav={user_wav}\n  ref_wav={ref_wav}")
            try:
                mg.process(
                    user_wav,   # target
                    ref_wav,    # reference
                    ai_mastered_path
                )
                print("[upload_file] mg.process completed successfully!")
                matchering_succeeded = True
            except Exception as e:
                print(f"[upload_file] Matchering error: {e}")
    else:
        print("[upload_file] No suitable reference found. Skipping AI match...")

    # 5) If AI matching fails or no reference, just copy user_wav => ai_mastered_path
    if not matchering_succeeded:
        try:
            print("[upload_file] Fallback: copy user WAV to ai_mastered.wav and do final mastering chain.")
            AudioSegment.from_wav(user_wav).export(ai_mastered_path, format="wav")
        except Exception as e:
            print(f"[upload_file] Fallback copy error: {e}")
            return jsonify({"error": "Fallback copy error"}), 500

    # 6) Now apply final mastering chain (EQ + compression + -14 LUFS)
    final_path = os.path.join(PROCESSED_FOLDER, f"final_{mix_type}.wav")
    if not final_mastering_chain(ai_mastered_path, final_path):
        return jsonify({"error": "Final mastering chain failed"}), 500

    # 7) Return final file
    return send_file(final_path, as_attachment=True)

# Decide port
port = 8080 if not is_port_in_use(8080) else 5001

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=port)
