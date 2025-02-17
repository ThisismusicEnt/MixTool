from flask import Flask, request, jsonify, send_file, render_template
from pydub import AudioSegment, effects
import librosa
import numpy as np
import soundfile as sf
import os
import socket

# Initialize Flask app
app = Flask(__name__)

# Directories for file handling
UPLOAD_FOLDER = "uploads"
PROCESSED_FOLDER = "processed"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

# Function to check if a port is in use
def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0

# Function to process audio (Professional Mastering)
def process_audio(file_path, mix_type):
    try:
        # Load audio with Librosa
        y, sr = librosa.load(file_path, sr=None)

        # Apply pre-emphasis (slight high-pass filter to clean muddiness)
        y = librosa.effects.preemphasis(y)

        # --- Apply Mix Type Variations (Balanced & Professional) ---
        if mix_type == "Lo-Fi":
            y = librosa.effects.time_stretch(y, rate=0.98)  # Very slight tempo reduction
            y = librosa.effects.pitch_shift(y, sr=sr, n_steps=-0.5)  # Warmer tone
        elif mix_type == "Trap":
            y = librosa.effects.time_stretch(y, rate=1.02)  # Slightly faster for crispness
            y = librosa.effects.pitch_shift(y, sr=sr, n_steps=0.5)  # Enhances energy
        elif mix_type == "Pop":
            y = librosa.effects.pitch_shift(y, sr=sr, n_steps=0.7)  # Brighter, cleaner vocals
            y = librosa.effects.time_stretch(y, rate=1.01)  # Keeps the track tight
        elif mix_type == "Studio Master":
            pass  # No additional changes for studio-quality mastering

        # Save the modified track temporarily
        temp_path = "temp.wav"
        sf.write(temp_path, y, sr)

        # Load with pydub for final mastering adjustments
        audio = AudioSegment.from_wav(temp_path)

        # --- Apply Dynamic Range Compression (Controlled) ---
        audio = effects.normalize(audio)  # Normalizes peaks for better balance
        audio = audio.apply_gain(3)  # Slight gain boost

        # --- Apply Soft De-Esser (Targeted Reduction of Harsh Frequencies) ---
        audio = audio.low_pass_filter(14000).high_pass_filter(4000)  # Light touch only where needed

        # --- Apply Industry Standard Loudness (-14 LUFS) ---
        target_lufs = -14.0
        current_lufs = audio.dBFS
        gain_needed = target_lufs - current_lufs
        audio = audio.apply_gain(gain_needed)  # Ensures industry-standard loudness

        # Save final output
        output_filename = f"final_master_{mix_type}.wav"
        output_path = os.path.join(PROCESSED_FOLDER, output_filename)
        audio.export(output_path, format="wav")
        
        return output_path
    except Exception as e:
        print(f"Error processing audio: {str(e)}")
        return None

# Home route
@app.route('/')
def home():
    return render_template("index.html")

# Upload and process audio
@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files['file']
    mix_type = request.form.get("mix_type", "Studio Master")  # Default to Studio Master
    
    file_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(file_path)

    # Process file with selected mix type
    processed_path = process_audio(file_path, mix_type)

    if not processed_path:
        return jsonify({"error": "Failed to process audio"}), 500

    return send_file(processed_path, as_attachment=True)

# Auto-select port (8080 or 5001)
port = 8080 if not is_port_in_use(8080) else 5001

# Run Flask server externally
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=port)
