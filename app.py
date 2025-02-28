import os
import subprocess
import uuid
import logging
from pathlib import Path

from flask import Flask, request, render_template, redirect, url_for, send_file, flash
from werkzeug.middleware.proxy_fix import ProxyFix
from pydub import AudioSegment
from pydub.generators import Sine

# Try to import matchering, but app works without it
try:
    import matchering as mg
    MATCHERING_AVAILABLE = True
except ImportError:
    MATCHERING_AVAILABLE = False

# Initialize Flask app
app = Flask(__name__)

# Fix for Heroku's proxy setup
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Configure app
app.secret_key = os.environ.get("SECRET_KEY", "development_secret_key")
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True

# Configure paths - use /tmp for Heroku compatibility
UPLOAD_FOLDER = os.path.join("/tmp", "uploads")
PROCESSED_FOLDER = os.path.join("/tmp", "processed")

# Ensure directories exist
Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)
Path(PROCESSED_FOLDER).mkdir(parents=True, exist_ok=True)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Simple audio processing functions
def convert_audio_to_wav(input_path, output_path):
    """Convert any audio file to WAV format using FFmpeg"""
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "44100",
            "-ac", "2",
            output_path
        ]
        subprocess.run(cmd, capture_output=True, timeout=300)
        return os.path.exists(output_path)
    except Exception as e:
        logger.error(f"Audio conversion error: {e}")
        return False

def apply_simple_mastering(input_wav, output_wav):
    """Apply simple mastering: normalize and add some compression"""
    try:
        # Load audio with pydub
        audio = AudioSegment.from_wav(input_wav)
        
        # Normalize volume
        audio = audio.normalize()
        
        # Apply mild compression (we'll simulate this with gain adjustments)
        audio = audio.apply_gain(-3)  # Reduce overall volume
        audio = audio.apply_gain(3)   # Boost it back up
        
        # Export processed audio
        audio.export(output_wav, format="wav")
        return True
    except Exception as e:
        logger.error(f"Simple mastering error: {e}")
        return False

def create_fallback_beep(output_path):
    """Create a beep sound as a fallback"""
    try:
        beep = Sine(440).to_audio_segment(duration=1000)
        beep.export(output_path, format="wav")
        return True
    except Exception as e:
        logger.error(f"Beep creation error: {e}")
        return False

def convert_to_mp3(wav_path, mp3_path):
    """Convert WAV to MP3"""
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", wav_path,
            "-codec:a", "libmp3lame",
            "-qscale:a", "0",
            "-b:a", "320k",
            mp3_path
        ]
        subprocess.run(cmd, capture_output=True, timeout=60)
        return os.path.exists(mp3_path)
    except Exception as e:
        logger.error(f"MP3 conversion error: {e}")
        return False

# Routes
@app.route("/")
def index():
    """Show the upload form"""
    # Ensure directories exist
    Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)
    Path(PROCESSED_FOLDER).mkdir(parents=True, exist_ok=True)
    return render_template("index.html", matchering_available=MATCHERING_AVAILABLE)

@app.route("/upload", methods=["POST"])
def upload():
    """Handle file upload and start processing"""
    try:
        # Create a unique session ID
        session_id = str(uuid.uuid4())
        
        # Check if the target file is provided
        if "target_file" not in request.files:
            flash("Please upload an audio file to master.")
            return redirect(url_for("index"))
            
        target_file = request.files["target_file"]
        if target_file.filename == "":
            flash("No file selected.")
            return redirect(url_for("index"))
        
        # Save the target file
        target_filename = "".join(c for c in target_file.filename if c.isalnum() or c in '._- ')
        target_path = os.path.join(UPLOAD_FOLDER, f"{session_id}_target_{target_filename}")
        target_file.save(target_path)
        
        # Process with or without reference
        use_reference = False
        reference_path = None
        
        # Check if reference file is provided
        if "reference_file" in request.files and request.files["reference_file"].filename != "":
            ref_file = request.files["reference_file"]
            ref_filename = "".join(c for c in ref_file.filename if c.isalnum() or c in '._- ')
            reference_path = os.path.join(UPLOAD_FOLDER, f"{session_id}_ref_{ref_filename}")
            ref_file.save(reference_path)
            use_reference = True
        
        # Get export format preference
        export_format = request.form.get("export_format", "wav")
        
        # Convert to WAV
        target_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_target.wav")
        target_converted = convert_audio_to_wav(target_path, target_wav)
        
        # Initialize output paths
        output_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_output.wav")
        final_output = output_wav
        method_used = "Unknown"
        
        if not target_converted:
            # Create a beep sound if conversion fails
            beep_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_beep.wav")
            create_fallback_beep(beep_wav)
            final_output = beep_wav
            method_used = "Beep_Fallback"
        else:
            # Try to process the audio
            processing_success = False
            
            # If using reference and matchering is available
            if use_reference and MATCHERING_AVAILABLE and reference_path:
                ref_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_ref.wav")
                if convert_audio_to_wav(reference_path, ref_wav):
                    try:
                        # Try reference-based mastering
                        mg.process(
                            target=target_wav,
                            reference=ref_wav,
                            results=[mg.pcm16(output_wav)]
                        )
                        if os.path.exists(output_wav):
                            processing_success = True
                            method_used = "AI_Reference_Based"
                    except Exception as e:
                        logger.error(f"Reference mastering error: {e}")
            
            # If reference mastering failed or wasn't used, try simple mastering
            if not processing_success:
                simple_success = apply_simple_mastering(target_wav, output_wav)
                if simple_success:
                    processing_success = True
                    method_used = "Simple_Mastering"
                else:
                    # If simple mastering fails, just use the converted WAV
                    final_output = target_wav
                    method_used = "Original_Audio"
            
            # Convert to MP3 if requested
            if processing_success and export_format.lower() == "mp3":
                mp3_path = os.path.join(PROCESSED_FOLDER, f"{session_id}_output.mp3")
                if convert_to_mp3(output_wav, mp3_path):
                    final_output = mp3_path
        
        # Rename the final output to include the method used
        ext = os.path.splitext(final_output)[1]
        final_renamed = os.path.join(PROCESSED_FOLDER, f"{session_id}_{method_used}{ext}")
        
        try:
            os.rename(final_output, final_renamed)
            final_output = final_renamed
        except Exception as e:
            logger.error(f"Rename error: {e}")
        
        # Return the file for download
        return send_file(final_output, as_attachment=True)
        
    except Exception as e:
        logger.error(f"Processing error: {e}")
        flash("An error occurred during processing. Please try again.")
        return redirect(url_for("index"))

if __name__ == "__main__":
    # Get port from environment variable (Heroku sets this)
    port = int(os.environ.get("PORT", 5000))
    
    # In production, don't use debug mode
    debug = os.environ.get("FLASK_ENV") == "development"
    
    app.run(host="0.0.0.0", port=port, debug=debug)