import os
import subprocess
import uuid

from flask import Flask, request, render_template, redirect, flash, url_for, send_file, after_this_request
import matchering as mg
import logging

app = Flask(__name__)
app.secret_key = "YOUR_SECRET_KEY"

# Create folders at runtime
os.makedirs("uploads", exist_ok=True)
os.makedirs("processed", exist_ok=True)

def ffmpeg_to_wav(input_path, output_path):
    """
    Convert any audio/video to 16-bit 44.1kHz WAV stereo using FFmpeg.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "44100",
        "-ac", "2",
        output_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def cleanup_file(path):
    try:
        os.remove(path)
        app.logger.info(f"Deleted file {path}")
    except Exception as e:
        app.logger.error(f"Could not delete file {path}: {e}")

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    if "target_file" not in request.files or "reference_file" not in request.files:
        flash("Please upload both target and reference files.")
        return redirect(url_for("home"))

    target_file = request.files["target_file"]
    reference_file = request.files["reference_file"]
    if target_file.filename == "" or reference_file.filename == "":
        flash("Please select valid files for both target and reference.")
        return redirect(url_for("home"))

    # Unique ID for session
    session_id = str(uuid.uuid4())

    target_filename = f"{session_id}_target_{target_file.filename}"
    reference_filename = f"{session_id}_ref_{reference_file.filename}"

    target_path = os.path.join("uploads", target_filename)
    ref_path = os.path.join("uploads", reference_filename)

    target_file.save(target_path)
    reference_file.save(ref_path)

    # Convert to WAV
    target_wav = os.path.join("processed", f"{session_id}_target.wav")
    ref_wav = os.path.join("processed", f"{session_id}_ref.wav")
    ffmpeg_to_wav(target_path, target_wav)
    ffmpeg_to_wav(ref_path, ref_wav)

    # Master with matchering
    master_wav = os.path.join("processed", f"{session_id}_master.wav")
    try:
        mg.process(
            target=target_wav,
            reference=ref_wav,
            results=[mg.pcm16(master_wav)]
        )
    except Exception as e:
        app.logger.error(f"Matchering error: {e}")
        flash("Error during AI mastering. Check logs or try different files.")
        return redirect(url_for("home"))

    # Export format (wav/mp3)
    export_format = request.form.get("export_format", "wav")
    final_output_path = master_wav
    if export_format == "mp3":
        final_mp3 = os.path.join("processed", f"{session_id}_master.mp3")
        cmd = [
            "ffmpeg", "-y",
            "-i", master_wav,
            "-codec:a", "libmp3lame", "-qscale:a", "2",
            final_mp3
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        final_output_path = final_mp3

    # Schedule cleanup of all intermediate files after response
    @after_this_request
    def remove_files(response):
        cleanup_file(target_path)
        cleanup_file(ref_path)
        cleanup_file(target_wav)
        cleanup_file(ref_wav)
        cleanup_file(master_wav)
        if final_output_path != master_wav:
            cleanup_file(master_wav)  # in case
        return response

    # Return final output file
    return send_file(final_output_path, as_attachment=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
