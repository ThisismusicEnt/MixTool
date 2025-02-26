import os
import subprocess
import uuid
import logging

from flask import Flask, request, render_template, redirect, url_for, flash, send_file, after_this_request
from pydub import AudioSegment
import matchering as mg

app = Flask(__name__)
app.secret_key = "YOUR_SECRET_KEY"

# Ensure folders exist
os.makedirs("uploads", exist_ok=True)
os.makedirs("processed", exist_ok=True)

############################################################
# UTILITY FUNCTIONS
############################################################

def ffmpeg_to_wav(input_path, output_path):
    """
    Convert any audio/video to 16-bit 44.1kHz WAV stereo using FFmpeg.
    If input has no valid audio stream, the resulting WAV might be 0 bytes.
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
    # Capture stderr for debugging
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        app.logger.error(f"FFmpeg error converting {input_path}:\n{proc.stderr.decode('utf-8')}")

def produce_short_beep(out_path):
    """
    Creates a 1-second beep track as a last-resort fallback if everything fails.
    """
    try:
        base = AudioSegment.silent(duration=1000)  # 1 second of silence
        # Overlay a 1-second 440 Hz tone
        tone = AudioSegment.sine(frequency=440, duration=1000)
        beep = base.overlay(tone)
        beep.export(out_path, format="wav")
        app.logger.info(f"Produced beep fallback at {out_path}")
    except Exception as e:
        app.logger.error(f"produce_short_beep error: {e}")

def final_auto_master_fallback(in_wav, out_wav):
    """
    A minimal auto-master fallback chain:
    - High-pass filter at ~40 Hz
    - pydub normalize to 0 dB
    - mild gain (here, +3dB)
    - final target ~ -12 dBFS
    Returns True if success, False if error.
    """
    try:
        audio = AudioSegment.from_wav(in_wav)
        audio = audio.high_pass_filter(40)
        audio = audio.apply_gain(-audio.dBFS)  # normalize to 0dB
        audio = audio.apply_gain(3)            # mild push
        target_lufs = -12.0
        gain_needed = target_lufs - audio.dBFS
        audio = audio.apply_gain(gain_needed)
        audio.export(out_wav, format="wav")
        return True
    except Exception as e:
        app.logger.error(f"Auto fallback error: {e}")
        return False

############################################################
# ROUTES
############################################################

@app.route("/")
def index():
    """
    Renders index.html with file inputs named 'target_file', 'reference_file',
    and a dropdown or radio for 'export_format' (wav/mp3).
    """
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    if "target_file" not in request.files or "reference_file" not in request.files:
        flash("Please upload both target and reference files.")
        return redirect(url_for("index"))

    target_file = request.files["target_file"]
    ref_file = request.files["reference_file"]
    if target_file.filename == "" or ref_file.filename == "":
        flash("No valid files selected.")
        return redirect(url_for("index"))

    session_id = str(uuid.uuid4())
    target_upload_path = os.path.join("uploads", f"{session_id}_target_{target_file.filename}")
    ref_upload_path = os.path.join("uploads", f"{session_id}_ref_{ref_file.filename}")

    target_file.save(target_upload_path)
    ref_file.save(ref_upload_path)

    # Convert both to WAV
    target_wav = os.path.join("processed", f"{session_id}_target.wav")
    ref_wav = os.path.join("processed", f"{session_id}_ref.wav")
    ffmpeg_to_wav(target_upload_path, target_wav)
    ffmpeg_to_wav(ref_upload_path, ref_wav)

    # Attempt AI mastering
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
        app.logger.info("AI mastering succeeded.")
    except Exception as e:
        app.logger.error(f"Matchering error: {e}")

    if not ai_success:
        # fallback auto mastering
        fallback_used = True
        success = final_auto_master_fallback(target_wav, master_wav)
        if not success:
            # fallback also failed => produce beep
            beep_wav = os.path.join("processed", f"{session_id}_beep.wav")
            produce_short_beep(beep_wav)
            master_wav = beep_wav
            app.logger.error("Both AI & fallback failed; produced beep fallback.")
        else:
            app.logger.info("Auto fallback completed master.")

    # Export format
    export_format = request.form.get("export_format", "wav")
    final_output_path = master_wav
    if export_format == "mp3":
        mp3_path = os.path.join("processed", f"{session_id}_master.mp3")
        cmd = [
            "ffmpeg", "-y",
            "-i", master_wav,
            "-codec:a", "libmp3lame", "-qscale:a", "2",
            mp3_path
        ]
        sp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if sp.returncode != 0:
            app.logger.error(f"FFmpeg MP3 conversion error: {sp.stderr.decode('utf-8')}")
            # If MP3 conversion fails, just keep WAV
        else:
            final_output_path = mp3_path

    # Rename final file to indicate which path was used
    if ai_success:
        label = "AI_Completed_Master"
    elif fallback_used:
        # If fallback used but also beep was needed, we can combine label
        # but let's keep it simple
        if os.path.basename(master_wav).endswith("_beep.wav") or os.path.basename(final_output_path).endswith("_beep.wav"):
            label = "Auto_Beep_Fallback"
        else:
            label = "Auto_Completed_Master"
    else:
        label = "Unknown"

    ext = ".mp3" if final_output_path.endswith(".mp3") else ".wav"
    final_renamed = os.path.join("processed", f"{session_id}_{label}{ext}")
    try:
        os.rename(final_output_path, final_renamed)
        final_output_path = final_renamed
    except Exception as e:
        app.logger.error(f"Rename final file error: {e}")

    @after_this_request
    def cleanup_files(response):
        # Remove intermediate
        for p in [
            target_upload_path,
            ref_upload_path,
            target_wav,
            ref_wav,
        ]:
            if os.path.exists(p):
                cleanup_file(p)
        # If fallback or AI created a 'master.wav' or beep, remove it if not final
        # We'll remove final_output_path last so we can serve it
        return response

    return send_file(final_output_path, as_attachment=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
