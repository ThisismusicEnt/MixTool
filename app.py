import os
import subprocess
import uuid
import logging

from flask import Flask, request, render_template, redirect, url_for, send_file, flash, after_this_request
from pydub import AudioSegment
from pydub.generators import Sine
import matchering as mg

app = Flask(__name__)
app.secret_key = "YOUR_SECRET_KEY"

# Make sure these directories exist
os.makedirs("uploads", exist_ok=True)
os.makedirs("processed", exist_ok=True)

############################################################
# UTILITY FUNCTIONS
############################################################

def cleanup_file(path):
    try:
        os.remove(path)
        app.logger.info(f"Deleted file {path}")
    except Exception as e:
        app.logger.error(f"Could not delete file {path}: {e}")

def ffmpeg_to_wav(input_path, output_path):
    """
    Convert any audio/video file to 16-bit 44.1kHz WAV stereo using FFmpeg.
    If FFmpeg fails (libpulse error or invalid data), we log stderr but still proceed.
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
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        app.logger.error(
            f"FFmpeg error converting {input_path}:\n{proc.stderr.decode('utf-8')}"
        )

def produce_short_beep(out_path):
    """
    Creates a 1-second beep track as final fallback
    using pydub.generators.Sine for 440 Hz tone.
    """
    try:
        beep = Sine(440).to_audio_segment(duration=1000)  # 1-sec, 440Hz
        beep.export(out_path, format="wav")
        app.logger.info(f"Produced beep fallback at {out_path}")
    except Exception as e:
        app.logger.error(f"produce_short_beep error: {e}")

def final_auto_master_fallback(in_wav, out_wav):
    """
    Minimal auto pop & polish:
    - High-pass 40Hz
    - normalize to 0 dB
    - +3 dB mild boost
    - final target ~ -12 dBFS
    Returns True if success, False if error
    """
    try:
        audio = AudioSegment.from_wav(in_wav)
        audio = audio.high_pass_filter(40)
        audio = audio.apply_gain(-audio.dBFS)  # normalize to 0 dB
        audio = audio.apply_gain(3)           # mild push => ~ -3 dBFS
        # final target = ~ -12 dBFS
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
    Render a form that asks for 'target_file', 'reference_file', 
    and possibly 'export_format' (wav/mp3).
    """
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    # 1) Validate fields
    if "target_file" not in request.files or "reference_file" not in request.files:
        flash("Please upload both target and reference.")
        return redirect(url_for("index"))
    target_file = request.files["target_file"]
    ref_file = request.files["reference_file"]
    if target_file.filename == "" or ref_file.filename == "":
        flash("No valid files selected.")
        return redirect(url_for("index"))

    # 2) Save them
    session_id = str(uuid.uuid4())
    target_path = os.path.join("uploads", f"{session_id}_target_{target_file.filename}")
    ref_path = os.path.join("uploads", f"{session_id}_ref_{ref_file.filename}")

    target_file.save(target_path)
    ref_file.save(ref_path)

    # 3) Convert to WAV
    target_wav = os.path.join("processed", f"{session_id}_target.wav")
    ref_wav = os.path.join("processed", f"{session_id}_ref.wav")
    ffmpeg_to_wav(target_path, target_wav)
    ffmpeg_to_wav(ref_path, ref_wav)

    # 4) Attempt AI Master
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
        app.logger.info("AI master success!")
    except Exception as e:
        app.logger.error(f"Matchering error: {e}")

    # 5) Fallback if AI fails
    if not ai_success:
        fallback_used = True
        success = final_auto_master_fallback(target_wav, master_wav)
        if not success:
            # 6) If fallback also fails => produce beep
            beep_wav = os.path.join("processed", f"{session_id}_beep.wav")
            produce_short_beep(beep_wav)
            master_wav = beep_wav
            app.logger.error("Both AI & fallback failed; beep fallback.")
        else:
            app.logger.info("Auto fallback completed master.")

    # 7) Convert to MP3 if requested
    export_format = request.form.get("export_format", "wav")
    final_output_path = master_wav
    if export_format == "mp3":
        mp3_path = os.path.join("processed", f"{session_id}_master.mp3")
        sp = subprocess.run([
            "ffmpeg", "-y",
            "-i", master_wav,
            "-codec:a", "libmp3lame", "-qscale:a", "2",
            mp3_path
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if sp.returncode != 0:
            app.logger.error(f"MP3 conversion error: {sp.stderr.decode('utf-8')}")
            # keep WAV
        else:
            final_output_path = mp3_path

    # 8) Rename final output => AI_Completed or Auto_Completed or Auto_Beep
    if ai_success:
        label = "AI_Completed_Master"
    elif fallback_used:
        # If beep is used or fallback is used
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

    # 9) Cleanup after response
    @after_this_request
    def cleanup_files(response):
        for p in [target_path, ref_path, target_wav, ref_wav]:
            if os.path.exists(p):
                cleanup_file(p)
        # We do NOT remove final_output_path here so user can download it
        return response

    # 10) Return final file
    return send_file(final_output_path, as_attachment=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
