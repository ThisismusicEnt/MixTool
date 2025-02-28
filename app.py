import os
import subprocess
import uuid
import logging
import time
import wave
import shutil
from pathlib import Path

from flask import Flask, request, render_template, redirect, url_for, send_file, flash, jsonify
from pydub import AudioSegment
from pydub.effects import normalize
from pydub.generators import Sine

# Optional import for reference-based mastering
try:
    import matchering as mg
    MATCHERING_AVAILABLE = True
except ImportError:
    MATCHERING_AVAILABLE = False

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "development_secret_key")

# Configure paths - use /tmp for Heroku compatibility
BASE_DIR = os.environ.get("AUDIO_STORAGE_PATH", "/tmp")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
PROCESSED_FOLDER = os.path.join(BASE_DIR, "processed")
LOG_FOLDER = os.path.join(BASE_DIR, "logs")

# Ensure directories exist
for directory in [UPLOAD_FOLDER, PROCESSED_FOLDER, LOG_FOLDER]:
    Path(directory).mkdir(parents=True, exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_FOLDER, 'app.log'))
    ]
)
logger = logging.getLogger(__name__)

############################################################
# AUDIO VALIDATION & CONVERSION
############################################################

def is_valid_audio_file(file_path):
    """Check if file exists, has content, and can be processed by FFmpeg"""
    if not os.path.exists(file_path):
        logger.error(f"File does not exist: {file_path}")
        return False
        
    if os.path.getsize(file_path) < 100:  # Arbitrary small size
        logger.error(f"File too small to be valid audio: {file_path}")
        return False
    
    # Try to get audio info with FFprobe
    try:
        cmd = [
            "ffprobe", 
            "-v", "error", 
            "-show_entries", "format=duration", 
            "-of", "default=noprint_wrappers=1:nokey=1", 
            file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0:
            logger.error(f"FFprobe couldn't analyze file: {file_path}")
            return False
            
        duration = float(result.stdout.strip())
        if duration < 0.1:  # Less than 100ms is probably not valid audio
            logger.error(f"Audio duration too short: {duration}s")
            return False
            
        logger.info(f"Valid audio file: {file_path}, duration: {duration}s")
        return True
    except Exception as e:
        logger.error(f"Error validating audio file: {str(e)}")
        return False

def validate_wav_file(file_path):
    """Check if WAV file is valid and get its properties"""
    try:
        with wave.open(file_path, 'rb') as wave_file:
            channels = wave_file.getnchannels()
            sample_width = wave_file.getsampwidth() 
            frame_rate = wave_file.getframerate()
            frames = wave_file.getnframes()
            
            if frames == 0:
                logger.warning(f"WAV file {file_path} has 0 frames")
                return False
                
            logger.info(f"WAV file validated: {file_path} - Channels: {channels}, "
                       f"Sample width: {sample_width}, Frame rate: {frame_rate}, "
                       f"Frames: {frames}")
            return True
    except Exception as e:
        logger.error(f"WAV validation error for {file_path}: {e}")
        return False

def convert_to_wav(input_path, output_path, normalize_audio=True):
    """Convert audio to WAV format with optional normalization"""
    logger.info(f"Converting {input_path} to WAV at {output_path}")
    
    if not os.path.exists(input_path):
        logger.error(f"Input file does not exist: {input_path}")
        return False
    
    # Normalizing options
    norm_filter = "loudnorm=I=-16:TP=-1.5:LRA=11" if normalize_audio else ""
    
    try:
        # Build the FFmpeg command
        cmd = ["ffmpeg", "-y", "-i", input_path]
        
        # Add filters if needed
        if norm_filter:
            cmd.extend(["-af", norm_filter])
        
        # Add output options and path
        cmd.extend([
            "-vn",                  # No video
            "-acodec", "pcm_s16le", # 16-bit PCM
            "-ar", "44100",         # 44.1kHz sample rate
            "-ac", "2",             # Stereo (2 channels)
            output_path
        ])
        
        # Run FFmpeg
        logger.debug(f"Running command: {' '.join(cmd)}")
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
        
        if proc.returncode != 0:
            stderr = proc.stderr.decode('utf-8')
            logger.error(f"FFmpeg error: {stderr}")
            return False
            
        # Validate output
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
            logger.error(f"Output file missing or too small: {output_path}")
            return False
            
        if not validate_wav_file(output_path):
            logger.error(f"Output WAV file is invalid: {output_path}")
            return False
            
        logger.info(f"Successfully converted to WAV: {output_path}")
        return True
        
    except subprocess.TimeoutExpired:
        logger.error(f"FFmpeg timed out processing {input_path}")
        return False
    except Exception as e:
        logger.error(f"Conversion error: {str(e)}")
        return False

def convert_to_mp3(wav_path, mp3_path, bitrate="320k"):
    """Convert WAV to MP3 format"""
    logger.info(f"Converting {wav_path} to MP3 at {mp3_path}")
    
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", wav_path,
            "-codec:a", "libmp3lame",
            "-qscale:a", "0",
            "-b:a", bitrate,
            mp3_path
        ]
        
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        
        if proc.returncode != 0:
            stderr = proc.stderr.decode('utf-8')
            logger.error(f"MP3 conversion error: {stderr}")
            return False
            
        if not os.path.exists(mp3_path) or os.path.getsize(mp3_path) < 1000:
            logger.error(f"MP3 output file missing or too small: {mp3_path}")
            return False
            
        logger.info(f"Successfully converted to MP3: {mp3_path}")
        return True
    except Exception as e:
        logger.error(f"MP3 conversion error: {str(e)}")
        return False

############################################################
# MASTERING FUNCTIONS
############################################################

def create_basic_beep(output_path):
    """Create a 1-second beep as final fallback"""
    try:
        beep = Sine(440).to_audio_segment(duration=1000)
        beep = beep.fade_in(50).fade_out(50)
        beep.export(output_path, format="wav")
        logger.info(f"Created beep at {output_path}")
        return True
    except Exception as e:
        logger.error(f"Beep creation error: {str(e)}")
        return False

def parameter_based_master(input_wav, output_wav, params):
    """
    Master audio based on user-defined parameters.
    
    Parameters:
    - input_wav: Path to input WAV file
    - output_wav: Path to output WAV file
    - params: Dictionary with mastering parameters:
        - bass_boost: Amount of bass boost (0-10)
        - brightness: Amount of high frequency enhancement (0-10)
        - loudness: Target loudness (-24 to -6 LUFS)
        - compression: Amount of compression (0-10)
        - stereo_width: Stereo enhancement (0-10)
    
    Returns:
    - True if successful, False otherwise
    """
    logger.info(f"Starting parameter-based mastering for {input_wav}")
    logger.info(f"Parameters: {params}")
    
    try:
        # Set default parameters if not provided
        bass_boost = min(max(int(params.get('bass_boost', 5)), 0), 10)
        brightness = min(max(int(params.get('brightness', 5)), 0), 10)
        target_loudness = min(max(float(params.get('loudness', -14)), -24), -6)
        compression = min(max(int(params.get('compression', 5)), 0), 10)
        stereo_width = min(max(int(params.get('stereo_width', 5)), 0), 10)
        
        # Get temp file paths for processing chain
        temp_dir = os.path.dirname(output_wav)
        base_name = os.path.basename(output_wav).split('.')[0]
        temp_eq = os.path.join(temp_dir, f"{base_name}_eq.wav")
        temp_comp = os.path.join(temp_dir, f"{base_name}_comp.wav")
        temp_loudness = os.path.join(temp_dir, f"{base_name}_loudness.wav")
        
        # 1. Apply EQ (bass boost and brightness)
        # Map 0-10 values to actual frequencies and gains
        bass_freq = 100  # Hz
        bass_gain = bass_boost * 1.5  # dB
        
        treble_freq = 8000  # Hz
        treble_gain = brightness * 1.0  # dB
        
        # Create EQ filter string
        eq_filter = f"equalizer=f={bass_freq}:t=o:w=1:g={bass_gain},equalizer=f={treble_freq}:t=o:w=1:g={treble_gain}"
        
        # Run EQ FFmpeg command
        eq_cmd = [
            "ffmpeg", "-y",
            "-i", input_wav,
            "-af", eq_filter,
            temp_eq
        ]
        subprocess.run(eq_cmd, capture_output=True, timeout=60)
        
        # 2. Apply compression
        # Map 0-10 compression to actual compression settings
        comp_ratio = 1 + (compression * 0.5)  # 1 to 6
        comp_threshold = -20 - (compression * 2)  # -20 to -40 dB
        comp_attack = 50 - (compression * 4)  # 50 to 10 ms (faster with higher compression)
        comp_release = 500 - (compression * 30)  # 500 to 200 ms
        
        comp_filter = f"acompressor=threshold={comp_threshold}dB:ratio={comp_ratio}:attack={comp_attack}:release={comp_release}:makeup=1"
        
        comp_cmd = [
            "ffmpeg", "-y",
            "-i", temp_eq,
            "-af", comp_filter,
            temp_comp
        ]
        subprocess.run(comp_cmd, capture_output=True, timeout=60)
        
        # 3. Apply stereo width if requested
        if stereo_width > 5:  # Only apply if width is above default
            # Map 0-10 stereo width to actual stereo width values
            width_amount = 1.0 + ((stereo_width - 5) * 0.1)  # 1.0 to 1.5
            stereo_filter = f"stereotools=mlevels=0.5:mpan=0.5:mode=lr>lr:sbal={width_amount}"
            
            width_cmd = [
                "ffmpeg", "-y",
                "-i", temp_comp,
                "-af", stereo_filter,
                temp_loudness
            ]
            subprocess.run(width_cmd, capture_output=True, timeout=60)
        else:
            # Skip this step if no width enhancement needed
            shutil.copy2(temp_comp, temp_loudness)
        
        # 4. Apply final loudness normalization
        # Map target loudness parameter to actual LUFS value
        loudnorm_filter = f"loudnorm=I={target_loudness}:TP=-1:LRA=11"
        
        loudness_cmd = [
            "ffmpeg", "-y",
            "-i", temp_loudness,
            "-af", loudnorm_filter,
            output_wav
        ]
        subprocess.run(loudness_cmd, capture_output=True, timeout=60)
        
        # Check if output file exists and is valid
        if os.path.exists(output_wav) and validate_wav_file(output_wav):
            logger.info(f"Parameter-based mastering completed successfully: {output_wav}")
            
            # Clean up temp files
            for temp_file in [temp_eq, temp_comp, temp_loudness]:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                    
            return True
        else:
            logger.error(f"Failed to create valid output file: {output_wav}")
            return False
            
    except Exception as e:
        logger.error(f"Parameter-based mastering error: {str(e)}")
        return False

def reference_based_master(target_wav, reference_wav, output_wav):
    """Master audio using the reference track (with matchering)"""
    logger.info(f"Starting reference-based mastering for {target_wav}")
    
    if not MATCHERING_AVAILABLE:
        logger.error("Matchering library is not available")
        return False
    
    try:
        # Configure matchering with more forgiving settings
        mg.configure(
            implementation=mg.HandlerbarsImpl(),
            result_bitrate=320,
            preview_size=30,
            threshold=-40,  # More permissive threshold
            tolerance=0.2   # More permissive tolerance
        )
        
        # Process with matchering
        mg.process(
            target=target_wav,
            reference=reference_wav,
            results=[mg.pcm16(output_wav)]
        )
        
        # Validate output
        if os.path.exists(output_wav) and validate_wav_file(output_wav):
            logger.info(f"Reference-based mastering completed successfully: {output_wav}")
            return True
        else:
            logger.error(f"Failed to create valid output file: {output_wav}")
            return False
    except Exception as e:
        logger.error(f"Reference-based mastering error: {str(e)}")
        return False

def simple_loudness_master(input_wav, output_wav, target_lufs=-14):
    """Simple mastering with just loudness normalization"""
    logger.info(f"Starting simple loudness mastering for {input_wav}")
    
    try:
        # Use FFmpeg for simple loudness normalization
        cmd = [
            "ffmpeg", "-y",
            "-i", input_wav,
            "-af", f"loudnorm=I={target_lufs}:TP=-1:LRA=11",
            output_wav
        ]
        
        subprocess.run(cmd, capture_output=True, timeout=60)
        
        # Validate output
        if os.path.exists(output_wav) and validate_wav_file(output_wav):
            logger.info(f"Simple loudness mastering completed successfully: {output_wav}")
            return True
        else:
            logger.error(f"Failed to create valid output file: {output_wav}")
            return False
    except Exception as e:
        logger.error(f"Simple loudness mastering error: {str(e)}")
        return False

def copy_as_fallback(input_path, output_path):
    """Copy input file as a fallback when all else fails"""
    try:
        shutil.copy2(input_path, output_path)
        logger.info(f"Copied input file as fallback: {output_path}")
        return True
    except Exception as e:
        logger.error(f"Copy fallback error: {str(e)}")
        return False

############################################################
# MAIN MASTERING FUNCTION
############################################################

def master_audio(session_id, input_file, reference_file=None, params=None, export_format="wav"):
    """
    Main mastering function that handles the entire process.
    
    Parameters:
    - session_id: Unique session ID
    - input_file: Path to input audio file
    - reference_file: Path to reference audio file (optional)
    - params: Mastering parameters for parameter-based mastering
    - export_format: Output format ("wav" or "mp3")
    
    Returns:
    - Path to the mastered file if successful, None otherwise
    - Method used for mastering
    """
    logger.info(f"Starting mastering process for session {session_id}")
    
    if not params:
        params = {}
    
    # Determine mastering method
    use_reference = reference_file is not None and os.path.exists(reference_file) and params.get('use_reference', True)
    method = "reference" if use_reference else "parameter"
    
    # Create path names
    input_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_input.wav")
    ref_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_reference.wav") if reference_file else None
    output_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_master.wav")
    
    # Master method used (for filename)
    mastering_method_used = "Unknown"
    
    # Convert input to WAV
    input_converted = convert_to_wav(input_file, input_wav)
    if not input_converted:
        logger.error(f"Failed to convert input file to WAV: {input_file}")
        # Create beep as fallback
        beep_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_beep.wav")
        create_basic_beep(beep_wav)
        mastering_method_used = "Beep_Fallback"
        output_wav = beep_wav
    else:
        # Convert reference to WAV if needed
        ref_converted = False
        if use_reference and reference_file:
            ref_converted = convert_to_wav(reference_file, ref_wav)
            if not ref_converted:
                logger.warning(f"Failed to convert reference file to WAV: {reference_file}")
                method = "parameter"  # Fall back to parameter-based mastering
        
        # Perform mastering based on method
        mastering_success = False
        
        if method == "reference" and ref_converted:
            # Try reference-based mastering
            mastering_success = reference_based_master(input_wav, ref_wav, output_wav)
            if mastering_success:
                mastering_method_used = "AI_Reference_Based"
            else:
                logger.warning("Reference-based mastering failed, falling back to parameter-based")
                method = "parameter"
        
        if method == "parameter":
            # Try parameter-based mastering
            mastering_success = parameter_based_master(input_wav, output_wav, params)
            if mastering_success:
                mastering_method_used = "Parameter_Based"
            else:
                logger.warning("Parameter-based mastering failed, falling back to simple loudness")
                
                # Try simple loudness mastering as fallback
                simple_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_simple.wav")
                simple_success = simple_loudness_master(input_wav, simple_wav)
                
                if simple_success:
                    output_wav = simple_wav
                    mastering_success = True
                    mastering_method_used = "Simple_Loudness"
                else:
                    # Last resort: just copy the input file
                    copy_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_copy.wav")
                    copy_success = copy_as_fallback(input_wav, copy_wav)
                    
                    if copy_success:
                        output_wav = copy_wav
                        mastering_success = True
                        mastering_method_used = "Original_Copy"
                    else:
                        # Ultimate fallback: create a beep
                        beep_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_beep.wav")
                        create_basic_beep(beep_wav)
                        output_wav = beep_wav
                        mastering_success = True
                        mastering_method_used = "Beep_Fallback"
    
    # Convert to MP3 if requested
    final_output = output_wav
    
    if export_format.lower() == "mp3":
        mp3_path = os.path.join(PROCESSED_FOLDER, f"{session_id}_master.mp3")
        mp3_success = convert_to_mp3(output_wav, mp3_path)
        
        if mp3_success:
            final_output = mp3_path
        else:
            logger.warning("MP3 conversion failed, keeping WAV output")
    
    # Rename final output with method used
    ext = os.path.splitext(final_output)[1]
    final_renamed = os.path.join(PROCESSED_FOLDER, f"{session_id}_{mastering_method_used}{ext}")
    
    try:
        os.rename(final_output, final_renamed)
        final_output = final_renamed
        logger.info(f"Final output renamed to: {final_output}")
    except Exception as e:
        logger.error(f"Rename final file error: {str(e)}")
    
    return final_output, mastering_method_used

def cleanup_session_files(session_id, keep_files=None):
    """Clean up temporary files from a session"""
    if keep_files is None:
        keep_files = []
    
    for folder in [UPLOAD_FOLDER, PROCESSED_FOLDER]:
        if os.path.exists(folder):
            for filename in os.listdir(folder):
                if session_id in filename and os.path.join(folder, filename) not in keep_files:
                    try:
                        os.remove(os.path.join(folder, filename))
                        logger.debug(f"Removed temporary file: {filename}")
                    except Exception as e:
                        logger.error(f"Error removing file {filename}: {str(e)}")

############################################################
# ROUTES
############################################################

@app.route("/")
def index():
    """Show the main upload form"""
    return render_template("index.html", matchering_available=MATCHERING_AVAILABLE)

@app.route("/upload", methods=["POST"])
def upload():
    """Handle file upload and start mastering process"""
    # Create session ID
    session_id = str(uuid.uuid4())
    logger.info(f"Starting new upload, session ID: {session_id}")
    
    # Get target file
    if "target_file" not in request.files:
        flash("Please upload a target audio file.")
        return redirect(url_for("index"))
    
    target_file = request.files["target_file"]
    if target_file.filename == "":
        flash("No file selected.")
        return redirect(url_for("index"))
    
    # Create safe filenames
    target_filename = "".join(c for c in target_file.filename if c.isalnum() or c in '._- ')
    target_path = os.path.join(UPLOAD_FOLDER, f"{session_id}_target_{target_filename}")
    
    # Save target file
    logger.info(f"Saving target file to: {target_path}")
    target_file.save(target_path)
    
    # Validate target file
    if not is_valid_audio_file(target_path):
        flash("Invalid audio file. Please upload a valid audio file.")
        return redirect(url_for("index"))
    
    # Get reference file if provided
    reference_path = None
    if "reference_file" in request.files:
        ref_file = request.files["reference_file"]
        if ref_file.filename != "":
            ref_filename = "".join(c for c in ref_file.filename if c.isalnum() or c in '._- ')
            reference_path = os.path.join(UPLOAD_FOLDER, f"{session_id}_ref_{ref_filename}")
            logger.info(f"Saving reference file to: {reference_path}")
            ref_file.save(reference_path)
    
    # Get mastering parameters
    mastering_method = request.form.get("mastering_method", "parameter")
    export_format = request.form.get("export_format", "wav")
    
    # Get parameter-based mastering parameters
    params = {
        "use_reference": mastering_method == "reference",
        "bass_boost": int(request.form.get("bass_boost", 5)),
        "brightness": int(request.form.get("brightness", 5)),
        "loudness": float(request.form.get("loudness", -14)),
        "compression": int(request.form.get("compression", 5)),
        "stereo_width": int(request.form.get("stereo_width", 5))
    }
    
    # Perform mastering
    output_path, method_used = master_audio(
        session_id, 
        target_path, 
        reference_path, 
        params, 
        export_format
    )
    
    # Clean up temporary files except the output
    cleanup_session_files(session_id, keep_files=[output_path])
    
    # Return the file for download
    if output_path and os.path.exists(output_path):
        logger.info(f"Sending file to user: {output_path}")
        return send_file(output_path, as_attachment=True)
    else:
        flash("Error processing audio. Please try again.")
        return redirect(url_for("index"))

############################################################
# ERROR HANDLING
############################################################

@app.errorhandler(Exception)
def handle_error(e):
    """Global error handler"""
    logger.error(f"Unhandled exception: {str(e)}")
    flash("An unexpected error occurred. Please try again.")
    return redirect(url_for("index"))

############################################################
# MAIN
############################################################

if __name__ == "__main__":
    # Get port from environment variable (Heroku sets this)
    port = int(os.environ.get("PORT", 5000))
    
    # In production, don't use debug mode
    debug = os.environ.get("FLASK_ENV") == "development"
    
    app.run(host="0.0.0.0", port=port, debug=debug)