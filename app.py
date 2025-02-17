from flask import Flask, request, jsonify, send_file
from pydub import AudioSegment
import librosa
import numpy as np
import soundfile as sf
import os

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
PROCESSED_FOLDER = "processed"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)

def process_audio(file_path):
    # Load audio
    y, sr = librosa.load(file_path, sr=None)

    # Apply simple audio enhancements (EQ, compression simulation)
    y = librosa.effects.preemphasis(y)

    # Pitch shift (+2 semitones as remix effect)
    y = librosa.effects.pitch_shift(y, sr, n_steps=2)

    # Save new file
    output_path = os.path.join(PROCESSED_FOLDER, "remixed_" + os.path.basename(file_path))
    sf.write(output_path, y, sr)
    return output_path

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files['file']
    file_path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(file_path)

    # Process the file
    processed_path = process_audio(file_path)

    return send_file(processed_path, as_attachment=True)

if __name__ == '__main__':
    app.run(debug=True)
