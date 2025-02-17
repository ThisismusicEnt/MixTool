from flask import Flask, request, jsonify, send_file, render_template
from pydub import AudioSegment
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

# Function to process audio (remix & master)
def process_audio(file_path):
    try:
        # Load audio
        y, sr = librosa.load(file_path, sr=None)

        # Apply enhancements (EQ, slight compression, stereo widening)
        y = librosa.effects.preemphasis(y)

        # FIX: Correct pitch shifting syntax
        y = librosa.effects.pitch_shift(y=y, sr=sr, n_steps=2)

        # FIX: Ensure file exists before saving
        output_filename = "remixed_" + os.path.basename(file_path)
        output_path = os.path.join(PROCESSED_FOLDER, output_filename)
        sf.write(output_path, y, sr)
        
        if not os.path.exists(output_path):
            return None  # Return None if file wasn't created

        return output_path
    except Exception as e:
        print(f"Error processing audio: {str(e)}")  # Debugging
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
    file_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(file_path)

    # Process file
    processed_path = process_audio(file_path)

    if not processed_path:
        return jsonify({"error": "Failed to process audio"}), 500

    return send_file(processed_path, as_attachment=True)

# Auto-select port (8080 or 5001)
port = 8080 if not is_port_in_use(8080) else 5001

# Run Flask server externally
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=port)
