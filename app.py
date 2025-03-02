import os
import subprocess
import uuid
import logging
import time
from pathlib import Path

from flask import Flask, request, render_template, redirect, url_for, send_file, flash
from werkzeug.middleware.proxy_fix import ProxyFix
from pydub import AudioSegment
from pydub.generators import Sine

# Initialize Flask app
app = Flask(__name__)

# Fix for Heroku's proxy setup
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# Configure app
app.secret_key = os.environ.get("SECRET_KEY", "development_secret_key")
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB max upload

# Configure paths - use /tmp for Heroku compatibility
UPLOAD_FOLDER = os.path.join("/tmp", "uploads")
PROCESSED_FOLDER = os.path.join("/tmp", "processed")

# Ensure directories exist
Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)
Path(PROCESSED_FOLDER).mkdir(parents=True, exist_ok=True)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Special Heroku path for FFmpeg
FFMPEG_PATHS = [
    "ffmpeg",                         # Standard path
    "/app/vendor/ffmpeg/bin/ffmpeg",  # Heroku buildpack path
    "/usr/bin/ffmpeg",                # Alternate Linux path
    "/usr/local/bin/ffmpeg",          # Alternate macOS path
]

def get_ffmpeg_path():
    """Find the correct path for FFmpeg"""
    for path in FFMPEG_PATHS:
        try:
            result = subprocess.run([path, "-version"], 
                                   capture_output=True, 
                                   text=True, 
                                   timeout=5)
            if result.returncode == 0:
                logger.info(f"Found FFmpeg at: {path}")
                return path
        except Exception:
            continue
    
    logger.warning("FFmpeg not found in any standard location")
    return None

# Store FFmpeg path
FFMPEG_PATH = get_ffmpeg_path()

# Hybrid approach - try FFmpeg first, fall back to PyDub if needed
def process_audio(input_file, output_file, params=None):
    """Process audio using FFmpeg if available, otherwise PyDub"""
    if params is None:
        params = {}
    
    # Get parameters with defaults
    bass_boost = min(max(int(params.get('bass_boost', 5)), 0), 10)
    brightness = min(max(int(params.get('brightness', 5)), 0), 10)
    compression = min(max(int(params.get('compression', 5)), 0), 10)
    stereo_width = min(max(int(params.get('stereo_width', 5)), 0), 10)
    target_lufs = min(max(float(params.get('loudness', -14)), -24), -6)
    
    # Try FFmpeg first if available
    if FFMPEG_PATH:
        try:
            logger.info(f"Attempting FFmpeg processing with path: {FFMPEG_PATH}")
            
            # Map parameters to FFmpeg values
            bass_gain = (bass_boost - 5) * 3       # -15dB to +15dB
            treble_gain = (brightness - 5) * 2     # -10dB to +10dB
            
            # Higher compression = lower threshold and higher ratio
            comp_threshold = -24 - compression * 2  # -24dB to -44dB
            comp_ratio = 1.5 + compression * 0.3    # 1.5:1 to 4.5:1
            
            # Build filter chain
            filter_chain = []
            
            # 1. Add bass and treble EQ if not at default
            if bass_boost != 5 or brightness != 5:
                eq_filter = f"equalizer=f=100:t=q:w=1:g={bass_gain},equalizer=f=8000:t=q:w=1:g={treble_gain}"
                filter_chain.append(eq_filter)
            
            # 2. Add compression if not at minimum
            if compression > 0:
                comp_filter = f"acompressor=threshold={comp_threshold}dB:ratio={comp_ratio}:attack=20:release=250:makeup=2"
                filter_chain.append(comp_filter)
                
            # 3. Add stereo width if not at default
            if stereo_width != 5:
                width_factor = 0.5 + stereo_width * 0.1  # 0.5 to 1.5
                width_filter = f"stereotools=mlev={width_factor}"
                filter_chain.append(width_filter)
                
            # 4. Always add loudness normalization
            filter_chain.append(f"loudnorm=I={target_lufs}:TP=-1:LRA=11")
            
            # Join filters
            filter_string = ",".join(filter_chain)
            
            # Run FFmpeg
            cmd = [
                FFMPEG_PATH, "-y",
                "-i", input_file,
                "-af", filter_string,
                output_file
            ]
            
            logger.info(f"Running FFmpeg with command: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            
            if result.returncode != 0:
                logger.error(f"FFmpeg processing failed: {result.stderr}")
                raise Exception("FFmpeg processing failed")
                
            if os.path.exists(output_file) and os.path.getsize(output_file) > 1000:
                logger.info("FFmpeg processing successful")
                return "FFmpeg_Processing", True
            else:
                logger.error("FFmpeg produced invalid output file")
                raise Exception("Invalid output file")
                
        except Exception as e:
            logger.error(f"FFmpeg processing error: {str(e)}")
            logger.info("Falling back to PyDub processing")
            # Continue to PyDub processing
    
    # PyDub processing (fallback or if FFmpeg not available)
    try:
        logger.info("Processing with PyDub")
        
        # Load audio file
        audio = AudioSegment.from_file(input_file)
        
        # Ensure stereo
        if audio.channels == 1:
            audio = audio.set_channels(2)
        
        # 1. Apply bass boost
        if bass_boost != 5:
            bass_gain = (bass_boost - 5) * 3
            
            # Split and process frequencies
            bass_audio = audio.low_pass_filter(200)
            bass_audio = bass_audio.apply_gain(bass_gain)
            
            no_bass = audio.high_pass_filter(200)
            audio = bass_audio.overlay(no_bass)
        
        # 2. Apply brightness/treble
        if brightness != 5:
            treble_gain = (brightness - 5) * 2
            
            treble_audio = audio.high_pass_filter(5000)
            treble_audio = treble_audio.apply_gain(treble_gain)
            
            no_treble = audio.low_pass_filter(5000)
            audio = no_treble.overlay(treble_audio)
        
        # 3. Apply compression
        if compression > 0:
            # Normalize first to prepare for compression
            audio = audio.normalize()
            
            # Simple compression by flattening peaks
            threshold = -30 + ((10 - compression) * 2)  # -10dB to -30dB
            ratio = 1.5 + (compression * 0.25)  # 1.5:1 to 4:1
            
            # Process in chunks to simulate compression
            chunk_size = 100  # ms
            compressed = AudioSegment.empty()
            
            for i in range(0, len(audio), chunk_size):
                chunk = audio[i:i+chunk_size]
                chunk_db = chunk.dBFS
                
                if chunk_db > threshold:
                    # Calculate gain reduction
                    excess = chunk_db - threshold
                    reduction = excess * (1 - 1/ratio)
                    chunk = chunk.apply_gain(-reduction)
                
                compressed += chunk
            
            audio = compressed
            
            # Apply makeup gain
            audio = audio.apply_gain(compression * 0.5)
        
        # 4. Normalize to target loudness
        audio = audio.normalize()
        current_loudness = audio.dBFS
        loudness_adjustment = target_lufs - current_loudness
        audio = audio.apply_gain(loudness_adjustment)
        
        # Export processed audio
        audio.export(output_file, format="wav")
        
        if os.path.exists(output_file) and os.path.getsize(output_file) > 1000:
            logger.info("PyDub processing successful")
            return "PyDub_Processing", True
        else:
            logger.error("PyDub produced invalid output file")
            return "Processing_Failed", False
            
    except Exception as e:
        logger.error(f"PyDub processing error: {str(e)}")
        return "Processing_Failed", False

def create_fallback_beep(output_path):
    """Create a beep sound as a fallback"""
    try:
        beep = Sine(440).to_audio_segment(duration=1000)
        beep = beep.fade_in(50).fade_out(50)
        beep.export(output_path, format="wav")
        return True
    except Exception as e:
        logger.error(f"Beep creation error: {str(e)}")
        return False

# Routes
@app.route("/")
def index():
    """Show the upload form"""
    # Ensure directories exist
    Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)
    Path(PROCESSED_FOLDER).mkdir(parents=True, exist_ok=True)
    
    # Check FFmpeg availability on first request
    if not hasattr(app, 'ffmpeg_checked'):
        app.ffmpeg_checked = FFMPEG_PATH is not None
        logger.info(f"FFmpeg available: {app.ffmpeg_checked}")
    
    return render_template("index.html", matchering_available=False)

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
        
        logger.info(f"Target file saved: {target_path}")
        
        # Get mastering parameters
        params = {
            'bass_boost': int(request.form.get('bass_boost', 5)),
            'brightness': int(request.form.get('brightness', 5)),
            'compression': int(request.form.get('compression', 5)),
            'stereo_width': int(request.form.get('stereo_width', 5)),
            'loudness': float(request.form.get('loudness', -14))
        }
        
        # Get export format preference
        export_format = request.form.get("export_format", "wav")
        
        # Process the audio
        output_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_output.wav")
        method_used, processing_success = process_audio(target_path, output_wav, params)
        
        if processing_success:
            final_output = output_wav
            
            # Convert to MP3 if requested
            if export_format.lower() == "mp3":
                try:
                    mp3_path = os.path.join(PROCESSED_FOLDER, f"{session_id}_output.mp3")
                    
                    # Try FFmpeg for MP3 conversion if available
                    if FFMPEG_PATH:
                        cmd = [
                            FFMPEG_PATH, "-y",
                            "-i", output_wav,
                            "-codec:a", "libmp3lame",
                            "-qscale:a", "0",
                            "-b:a", "320k",
                            mp3_path
                        ]
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                        mp3_success = result.returncode == 0
                    else:
                        # Fall back to PyDub
                        audio = AudioSegment.from_wav(output_wav)
                        audio.export(mp3_path, format="mp3", bitrate="320k")
                        mp3_success = True
                    
                    if mp3_success and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 1000:
                        final_output = mp3_path
                        logger.info(f"Converted to MP3: {mp3_path}")
                    else:
                        logger.warning("MP3 conversion failed, using WAV instead")
                except Exception as e:
                    logger.error(f"MP3 conversion error: {str(e)}")
        else:
            # Create a beep as fallback
            beep_path = os.path.join(PROCESSED_FOLDER, f"{session_id}_beep.wav")
            create_fallback_beep(beep_path)
            final_output = beep_path
            method_used = "Beep_Fallback"
        
        # Rename the file to include the method
        ext = os.path.splitext(final_output)[1]
        final_renamed = os.path.join(PROCESSED_FOLDER, f"{session_id}_{method_used}{ext}")
        
        try:
            os.rename(final_output, final_renamed)
            final_output = final_renamed
        except Exception as e:
            logger.error(f"Rename error: {str(e)}")
        
        # Return the file for download
        return send_file(final_output, as_attachment=True, download_name=f"mastered_{target_filename}{ext}")
        
    except Exception as e:
        logger.error(f"Processing error: {str(e)}", exc_info=True)
        flash("An error occurred during processing. Please try again.")
        return redirect(url_for("index"))

if __name__ == "__main__":
    # Get port from environment variable (Heroku sets this)
    port = int(os.environ.get("PORT", 5000))
    
    # In production, don't use debug mode
    debug = os.environ.get("FLASK_ENV") == "development"
    
    app.run(host="0.0.0.0", port=port, debug=debug)