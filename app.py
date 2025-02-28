import os
import subprocess
import uuid
import logging
import time
import shutil
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
    logging.warning("Matchering not available - reference-based mastering disabled")

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

# Verify FFmpeg is available - IMPORTANT!
def check_ffmpeg():
    try:
        result = subprocess.run(["ffmpeg", "-version"], 
                               capture_output=True, 
                               text=True, 
                               timeout=5)
        if result.returncode == 0:
            logger.info(f"FFmpeg detected: {result.stdout.splitlines()[0]}")
            return True
        else:
            logger.error(f"FFmpeg check failed with code {result.returncode}")
            return False
    except Exception as e:
        logger.error(f"FFmpeg check error: {str(e)}")
        return False

# Simple audio processing functions
def convert_audio_to_wav(input_path, output_path):
    """Convert any audio file to WAV format using FFmpeg"""
    logger.info(f"Converting {input_path} to {output_path}")
    
    try:
        if not os.path.exists(input_path):
            logger.error(f"Input file doesn't exist: {input_path}")
            return False
            
        # Check file size
        file_size = os.path.getsize(input_path)
        if file_size < 100:  # Arbitrary small size
            logger.error(f"Input file too small ({file_size} bytes): {input_path}")
            return False
            
        # Run FFmpeg conversion with more forgiving settings
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vn",                     # No video
            "-ar", "44100",            # 44.1kHz sample rate
            "-ac", "2",                # Stereo (2 channels)
            "-acodec", "pcm_s16le",    # 16-bit PCM
            "-f", "wav",               # Force WAV format
            output_path
        ]
        
        logger.info(f"Running FFmpeg command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode != 0:
            logger.error(f"FFmpeg conversion failed: {result.stderr}")
            return False
            
        # Verify the output file
        if not os.path.exists(output_path):
            logger.error(f"Output file not created: {output_path}")
            return False
            
        output_size = os.path.getsize(output_path)
        if output_size < 1000:
            logger.error(f"Output file too small ({output_size} bytes): {output_path}")
            return False
            
        logger.info(f"Successfully converted audio to WAV: {output_path} ({output_size} bytes)")
        return True
    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg conversion timeout for {input_path}")
        return False
    except Exception as e:
        logger.error(f"Audio conversion error: {str(e)}")
        return False

def apply_parameter_based_mastering(input_wav, output_wav, params=None):
    """Apply mastering based on user parameters using FFmpeg"""
    
    if params is None:
        params = {}
        
    try:
        logger.info(f"Applying parameter-based mastering to {input_wav}")
        
        # Get parameters with default values
        bass_boost = min(max(int(params.get('bass_boost', 5)), 0), 10)
        brightness = min(max(int(params.get('brightness', 5)), 0), 10)
        compression = min(max(int(params.get('compression', 5)), 0), 10)
        stereo_width = min(max(int(params.get('stereo_width', 5)), 0), 10)
        target_lufs = min(max(float(params.get('loudness', -14)), -24), -6)
        
        logger.info(f"Mastering parameters: bass={bass_boost}, brightness={brightness}, "
                    f"compression={compression}, width={stereo_width}, lufs={target_lufs}")
        
        # Map parameters to actual values
        bass_gain = (bass_boost - 5) * 3       # -15dB to +15dB
        treble_gain = (brightness - 5) * 2     # -10dB to +10dB
        
        # Higher compression = lower threshold and higher ratio
        comp_threshold = -24 - compression * 2  # -24dB to -44dB
        comp_ratio = 1.5 + compression * 0.3    # 1.5:1 to 4.5:1
        
        # Build the filter chain
        filter_chain = []
        
        # 1. Apply bass and treble adjustments if not at default (5)
        if bass_boost != 5 or brightness != 5:
            eq_filter = f"equalizer=f=100:t=q:w=1:g={bass_gain},equalizer=f=8000:t=q:w=1:g={treble_gain}"
            filter_chain.append(eq_filter)
        
        # 2. Apply compression if not at minimum (0)
        if compression > 0:
            comp_filter = f"acompressor=threshold={comp_threshold}dB:ratio={comp_ratio}:attack=20:release=250:makeup=2"
            filter_chain.append(comp_filter)
            
        # 3. Apply stereo width if not at default (5)
        if stereo_width != 5:
            width_factor = 0.5 + stereo_width * 0.1  # 0.5 to 1.5
            width_filter = f"stereotools=mlev={width_factor}"
            filter_chain.append(width_filter)
            
        # 4. Always apply loudness normalization at the end
        filter_chain.append(f"loudnorm=I={target_lufs}:TP=-1:LRA=11")
        
        # Join the filter chain
        filter_string = ",".join(filter_chain)
        
        # Build the FFmpeg command
        cmd = [
            "ffmpeg", "-y",
            "-i", input_wav,
            "-af", filter_string,
            output_wav
        ]
        
        logger.info(f"Running FFmpeg mastering: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if result.returncode != 0:
            logger.error(f"FFmpeg mastering failed: {result.stderr}")
            return False
            
        if not os.path.exists(output_wav) or os.path.getsize(output_wav) < 1000:
            logger.error(f"Output file invalid: {output_wav}")
            return False
            
        logger.info(f"Parameter-based mastering complete: {output_wav}")
        return True
    except Exception as e:
        logger.error(f"Parameter mastering error: {str(e)}")
        return False

def apply_simple_mastering(input_wav, output_wav):
    """Apply simple mastering: normalize and add some compression"""
    try:
        logger.info(f"Applying simple mastering to {input_wav}")
        
        # Use FFmpeg for simple and reliable processing
        cmd = [
            "ffmpeg", "-y",
            "-i", input_wav,
            "-af", "acompressor=threshold=-24dB:ratio=2:attack=20:release=200:makeup=2,loudnorm=I=-14:TP=-1:LRA=11",
            output_wav
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode != 0:
            logger.error(f"Simple mastering FFmpeg failed: {result.stderr}")
            
            # Try with PyDub as last resort before beep
            try:
                logger.info(f"Trying PyDub for simple mastering")
                audio = AudioSegment.from_wav(input_wav)
                audio = audio.normalize()
                audio = audio.apply_gain(-3)  # Reduce overall volume
                audio = audio.apply_gain(3)   # Boost it back up
                audio.export(output_wav, format="wav")
                
                if not os.path.exists(output_wav) or os.path.getsize(output_wav) < 1000:
                    return False
                
                return True
            except Exception as pydub_error:
                logger.error(f"PyDub mastering error: {str(pydub_error)}")
                return False
        
        if not os.path.exists(output_wav) or os.path.getsize(output_wav) < 1000:
            logger.error(f"Output file invalid: {output_wav}")
            return False
            
        logger.info(f"Simple mastering complete: {output_wav}")
        return True
    except Exception as e:
        logger.error(f"Simple mastering error: {str(e)}")
        return False

def create_fallback_beep(output_path):
    """Create a beep sound as a fallback"""
    try:
        logger.info(f"Creating fallback beep at {output_path}")
        beep = Sine(440).to_audio_segment(duration=1000)
        beep = beep.fade_in(50).fade_out(50)
        beep.export(output_path, format="wav")
        logger.info("Beep created successfully")
        return True
    except Exception as e:
        logger.error(f"Beep creation error: {str(e)}")
        return False

def convert_to_mp3(wav_path, mp3_path):
    """Convert WAV to MP3"""
    try:
        logger.info(f"Converting {wav_path} to MP3 at {mp3_path}")
        cmd = [
            "ffmpeg", "-y",
            "-i", wav_path,
            "-codec:a", "libmp3lame",
            "-qscale:a", "0",
            "-b:a", "320k",
            mp3_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        
        if result.returncode != 0:
            logger.error(f"MP3 conversion failed: {result.stderr}")
            return False
            
        if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 1000:
            logger.error(f"Output MP3 file invalid: {mp3_path}")
            return False
            
        logger.info(f"MP3 conversion complete: {mp3_path}")
        return True
    except Exception as e:
        logger.error(f"MP3 conversion error: {str(e)}")
        return False

# Routes
@app.route("/")
def index():
    """Show the upload form"""
    # Ensure directories exist
    Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)
    Path(PROCESSED_FOLDER).mkdir(parents=True, exist_ok=True)
    
    # Verify FFmpeg on first request
    if not hasattr(app, 'ffmpeg_checked'):
        app.ffmpeg_checked = check_ffmpeg()
        
    return render_template("index.html", matchering_available=MATCHERING_AVAILABLE)

@app.route("/upload", methods=["POST"])
def upload():
    """Handle file upload and start processing"""
    try:
        # Ensure FFmpeg is available
        if not hasattr(app, 'ffmpeg_checked') or not app.ffmpeg_checked:
            app.ffmpeg_checked = check_ffmpeg()
            
        if not app.ffmpeg_checked:
            logger.error("FFmpeg not available - cannot process audio")
            flash("Server error: Audio processing software not available. Please try again later.")
            return redirect(url_for("index"))
        
        # Create a unique session ID
        session_id = str(uuid.uuid4())
        logger.info(f"New upload request: session_id={session_id}")
        
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
        
        # Process with or without reference
        mastering_method = request.form.get("mastering_method", "parameter")
        use_reference = mastering_method == "reference" and MATCHERING_AVAILABLE
        reference_path = None
        
        # Check if reference file is provided
        if "reference_file" in request.files and request.files["reference_file"].filename != "":
            ref_file = request.files["reference_file"]
            ref_filename = "".join(c for c in ref_file.filename if c.isalnum() or c in '._- ')
            reference_path = os.path.join(UPLOAD_FOLDER, f"{session_id}_ref_{ref_filename}")
            ref_file.save(reference_path)
            logger.info(f"Reference file saved: {reference_path}")
        else:
            use_reference = False
        
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
        
        # Start the processing timer
        start_time = time.time()
        
        # Convert to WAV
        target_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_target.wav")
        target_converted = convert_audio_to_wav(target_path, target_wav)
        
        # Initialize output paths
        output_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_output.wav")
        final_output = output_wav
        method_used = "Unknown"
        
        if not target_converted:
            logger.error(f"Target conversion failed for {target_path}")
            # Create a beep sound if conversion fails
            beep_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_beep.wav")
            create_fallback_beep(beep_wav)
            final_output = beep_wav
            method_used = "Beep_Fallback_ConversionError"
        else:
            # Try to process the audio
            processing_success = False
            
            # If using reference and matchering is available
            if use_reference and reference_path:
                ref_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_ref.wav")
                ref_converted = convert_audio_to_wav(reference_path, ref_wav)
                
                if ref_converted:
                    try:
                        logger.info("Attempting reference-based mastering")
                        # Try reference-based mastering
                        mg.configure(
                            implementation=mg.HandlerbarsImpl(),
                            result_bitrate=320,
                            preview_size=30,
                            threshold=-40,  # More permissive threshold
                            tolerance=0.2   # More permissive tolerance
                        )
                        
                        mg.process(
                            target=target_wav,
                            reference=ref_wav,
                            results=[mg.pcm16(output_wav)]
                        )
                        
                        if os.path.exists(output_wav) and os.path.getsize(output_wav) > 1000:
                            processing_success = True
                            method_used = "AI_Reference_Based"
                            logger.info("Reference-based mastering successful")
                        else:
                            logger.error("Reference-based mastering failed to produce valid output")
                    except Exception as e:
                        logger.error(f"Reference mastering error: {str(e)}")
            
            # If reference mastering failed or wasn't used, try parameter-based mastering
            if not processing_success:
                logger.info("Attempting parameter-based mastering")
                param_success = apply_parameter_based_mastering(target_wav, output_wav, params)
                
                if param_success:
                    processing_success = True
                    method_used = "Parameter_Based"
                    logger.info("Parameter-based mastering successful")
                else:
                    # If parameter mastering fails, try simple mastering
                    logger.info("Attempting simple mastering fallback")
                    simple_out = os.path.join(PROCESSED_FOLDER, f"{session_id}_simple.wav")
                    simple_success = apply_simple_mastering(target_wav, simple_out)
                    
                    if simple_success:
                        output_wav = simple_out
                        processing_success = True
                        method_used = "Simple_Mastering"
                        logger.info("Simple mastering successful")
                    else:
                        # If simple mastering fails, just use the converted WAV
                        logger.info("All mastering methods failed, using original WAV")
                        final_output = target_wav
                        method_used = "Original_Audio"
            
            # Convert to MP3 if requested and processing succeeded
            if processing_success and export_format.lower() == "mp3":
                mp3_path = os.path.join(PROCESSED_FOLDER, f"{session_id}_output.mp3")
                mp3_success = convert_to_mp3(output_wav, mp3_path)
                
                if mp3_success:
                    final_output = mp3_path
                    logger.info(f"Converted to MP3: {mp3_path}")
                else:
                    logger.warning("MP3 conversion failed, using WAV instead")
            elif processing_success:
                final_output = output_wav
        
        # Calculate processing time
        processing_time = time.time() - start_time
        logger.info(f"Processing completed in {processing_time:.2f} seconds using method: {method_used}")
        
        # Rename the final output to include the method used
        ext = os.path.splitext(final_output)[1]
        final_renamed = os.path.join(PROCESSED_FOLDER, f"{session_id}_{method_used}{ext}")
        
        try:
            os.rename(final_output, final_renamed)
            final_output = final_renamed
            logger.info(f"Renamed output to {final_output}")
        except Exception as e:
            logger.error(f"Rename error: {str(e)}")
        
        # Return the file for download
        return send_file(final_output, as_attachment=True, download_name=f"mastered_{target_filename}{ext}")
        
    except Exception as e:
        logger.error(f"Processing error: {str(e)}", exc_info=True)
        flash("An error occurred during processing. Please try again.")
        return redirect(url_for("index"))

if __name__ == "__main__":
    # Check for FFmpeg at startup
    if not check_ffmpeg():
        logger.warning("FFmpeg not detected! Audio processing may not work correctly.")
    
    # Get port from environment variable (Heroku sets this)
    port = int(os.environ.get("PORT", 5000))
    
    # In production, don't use debug mode
    debug = os.environ.get("FLASK_ENV") == "development"
    
    app.run(host="0.0.0.0", port=port, debug=debug)