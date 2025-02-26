import os
import subprocess
import uuid
import logging

from flask import Flask, request, render_template, redirect, flash, url_for, send_file, after_this_request
from pydub import AudioSegment
import matchering as mg

app = Flask(__name__)
app.secret_key = "YOUR_SECRET_KEY"

# Ensure these folders exist
os.makedirs("uploads", exist_ok=True)
os.makedirs("processed", exist_ok=True)

############################################################
# UTILITY FUNCTIONS
############################################################

def ffmpeg_to_wav(input_path, output_path):
    """
    Convert any audio/video file to 16-bit 44.1kHz WAV stereo using FFmpeg.
    If input has no valid audio stream, the resulting WAV might be invalid (0 bytes).
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

def validate_wav_is_audio(wav_path):
    """
    Check if the given WAV file actually contains audio frames.
    Returns True if valid, False if empty or invalid.
    """
    try:
        audio = AudioSegment.from_wav(wav_path)
        if len(audio) == 0:
            raise ValueError("No audio frames found.")
        return True
    except Exception as e:
        app.logger.error(f"WAV validation error for {wav_path}: {e}")
        return False

def cleanup_file(path):
    try:
        os.remove(path)
        app.logger.info(f"Deleted file {path}")
    except Exception as e:
        app.logger.error(f"Could not delete file {path}: {e}")

def final_auto_master_fallback(in_wav, out_wav):
    """
    A minimal auto-master fallback if matchering not possible:
    - high-pass at ~40 Hz
    - normalize (pydub)
    - apply mild gain
    - final loudness target ~ -12 dBFS
    """
    try:
        audio = AudioSegment.from_wav(in_wav)
        audio = audio.high_pass_filter(40)
        audio = audio.apply_gain(-audio.dBFS)  # normalize to 0 dB
        audio = audio.apply_gain(3)            # mild gain => ~ -3 dBFS
        # or set a final target like -12 dBFS
        target_lufs = -12.0
        gain_needed = target_lufs - audio.dBFS
        audio = audio.apply_gain(gain_needed)

        audio.export(out_wav, format="wav")
        return True
    except Exception as e:
        app.logger.error(f"Auto mastering fallback error: {e}")
        return False

############################################################
# ROUTES
############################################################

@app.route("/")
def index():
    """
    Main route for the homepage. 
    Renders index.html (should have form with 'target_file', 'reference_file', 'export_format').
    """
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    if "target_file" not in request.files:
        flash("No target file uploaded.")
        return redirect(url_for("index"))
    if "reference_file" not in request.files:
        flash("No reference file uploaded.")
        return redirect(url_for("index"))

    target_file = request.files["target_file"]
    reference_file = request.files["reference_file"]

    if target_file.filename == "" or reference_file.filename == "":
        flash("Please select valid target & reference files.")
        return redirect(url_for("index"))

    # Unique session ID
    session_id = str(uuid.uuid4())
    target_filename = f"{session_id}_target_{target_file.filename}"
    ref_filename = f"{session_id}_ref_{reference_file.filename}"

    target_path = os.path.join("uploads", target_filename)
    ref_path = os.path.join("uploads", ref_filename)

    target_file.save(target_path)
    reference_file.save(ref_path)

    # Convert both to WAV
    target_wav = os.path.join("processed", f"{session_id}_target.wav")
    ref_wav = os.path.join("processed", f"{session_id}_ref.wav")

    ffmpeg_to_wav(target_path, target_wav)
    ffmpeg_to_wav(ref_path, ref_wav)

    # Validate the WAVs have real audio
    if not validate_wav_is_audio(target_wav):
        flash("Target file has no valid audio. Please try a different file.")
        return redirect(url_for("index"))
    if not validate_wav_is_audio(ref_wav):
        flash("Reference file has no valid audio. Please try a different file.")
        return redirect(url_for("index"))

    # Attempt AI Mastering
    master_wav = os.path.join("processed", f"{session_id}_master.wav")
    ai_success = False
    fallback_used = False

    try:
        mg.process(
            target=target_wav,
            reference=ref_wav,
            results=[mg.pcm16(master_wav)]
        )
        ai_success = True
        app.logger.info("AI completed master.")
    except Exception as e:
        app.logger.error(f"Matchering error: {e}")

    if not ai_success:
        # fallback to auto pop & polish
        fallback_used = True
        app.logger.info("Attempting auto fallback...")
        success_fallback = final_auto_master_fallback(target_wav, master_wav)
        if not success_fallback:
            # if fallback also fails, show error
            flash("ERROR 404: Both AI and fallback auto mastering failed.")
            return redirect(url_for("index"))
        else:
            app.logger.info("Auto completed master.")

    # Export format
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

    # Label the final file so user knows which method succeeded
    if ai_success:
        result_label = "AI_Completed_Master"
    elif fallback_used:
        result_label = "Auto_Completed_Master"
    else:
        # Shouldn't happen, but just in case
        result_label = "Unknown"

    base_ext = ".mp3" if export_format == "mp3" else ".wav"
    final_renamed = os.path.join("processed", f"{session_id}_{result_label}{base_ext}")
    os.rename(final_output_path, final_renamed)
    final_output_path = final_renamed

    # After response, cleanup all intermediate files
    @after_this_request
    def remove_files(response):
        cleanup_file(target_path)
        cleanup_file(ref_path)
        cleanup_file(target_wav)
        cleanup_file(ref_wav)
        cleanup_file(master_wav)  # in case leftover
        if final_output_path != master_wav:
            cleanup_file(master_wav)
        return response

    return send_file(final_output_path, as_attachment=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
