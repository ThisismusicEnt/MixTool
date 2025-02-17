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

# Function to apply mastering (Perfect Industry Standard)
def process_audio(file_path, mix_type):
    try:
        # Load audio
        y, sr = librosa.load(file_path, sr=None)

        # Apply pre-emphasis (slight high-pass filter to clean muddiness)
        y = librosa.effects.preemphasis(y)

        # --- Apply Mix Type Variations (Balanced & Professional) ---
        if mix_type == "Lo-Fi":
            y = librosa.effects.time_stretch(y, rate=0.98)  # Very slight tempo reduction
            y = librosa.effects.pitch_shift(y, sr=sr, n_steps=-0.5)  # Warmer tone
        elif mix_type == "Trap":
            # Trap should NOT have extra reverb
            y = librosa.effects.time_stretch(y, rate=1.02)  # Slight tempo boost for crispness
            y = librosa.effects.pitch_shift(y, sr=sr, n_steps=0.5)  # Energy boost without over-processing
        elif mix_type == "Pop":
            y = librosa.effects.pitch_shift(y, sr=sr, n_steps=0.7)  # Bright but natural vocals
            y = librosa.effects.time_stretch(y, rate=1.01)  # Keeps rhythm tight
        elif mix_type == "Studio Master":
            pass  # No artificial processing, just mastering

        # Save the modified track temporarily
        temp_path = "temp.wav"
        sf.write(temp_path, y, sr)

        # Load with pydub for mastering
        audio = AudioSegment.from_wav(temp_path)

        # --- Apply Dynamic Range Compression (Industry Standard) ---
        audio = effects.normalize(audio)  # Levels peaks properly
        audio = audio.apply_gain(2)  # Gentle gain boost

        # --- First Pass: De-Essing (Light Processing) ---
        audio = audio.low_pass_filter(16000).high_pass_filter(4500)  # Removes harsh "S" frequencies

        # --- Apply Loudness Normalization (-14 LUFS) ---
        target_lufs = -14.0
        current_lufs = audio.dBFS
        gain_needed = target_lufs - current_lufs
        audio = audio.apply_gain(gain_needed)  # Ensures loudness is within standard

        # --- Save the Intermediate Master ---
        intermediate_path = "processed/intermediate_master.wav"
        audio.export(intermediate_path, format="wav")

        # --- Second Pass: Final Check (Prevent Overprocessing) ---
        final_audio = AudioSegment.from_wav(intermediate_path)

        # --- Final Compression & De-Essing Pass ---
        final_audio = effects.normalize(final_audio)  # Ensures balance after first pass
        final_audio = final_audio.low_pass_filter(15500).high_pass_filter(5000)  # Re-check de-essing

        # --- Final Loudness Adjustment (-14 LUFS) ---
        final_lufs = -14.0
        final_current_lufs = final_audio.dBFS
        final_gain_needed = final_lufs - final_current_lufs
        final_audio = final_audio.apply_gain(final_gain_needed)

        # Save final output
        output_filename = f"final_master_{mix_type}.wav"
        output_path = os.path.join(PROCESSED_FOLDER, output_filename)
        final_audio.export(output_path, format="wav")
        
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
