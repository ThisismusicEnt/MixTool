import os
import subprocess
import uuid
import logging
import time
import wave
import shutil
from pathlib import Path

from flask import Flask, request, render_template, redirect, url_for, send_file, flash, after_this_request
from pydub import AudioSegment
from pydub.generators import Sine
import matchering as mg

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "YOUR_SECRET_KEY")

# Use directories that will work with Heroku's ephemeral filesystem
# Create these directories at app startup
UPLOAD_FOLDER = os.path.join("/tmp", "uploads")
PROCESSED_FOLDER = os.path.join("/tmp", "processed")
LOG_FOLDER = os.path.join("/tmp", "logs")

# Ensure directories exist
Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)
Path(PROCESSED_FOLDER).mkdir(parents=True, exist_ok=True)
Path(LOG_FOLDER).mkdir(parents=True, exist_ok=True)

# Configure more detailed logging
file_handler = logging.FileHandler(os.path.join(LOG_FOLDER, 'app.log'))
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
app.logger.addHandler(file_handler)
app.logger.setLevel(logging.DEBUG)

############################################################
# UTILITY FUNCTIONS
############################################################

def cleanup_file(path):
    """Safely delete a file if it exists"""
    try:
        if os.path.exists(path):
            os.remove(path)
            app.logger.info(f"Deleted file {path}")
    except Exception as e:
        app.logger.error(f"Could not delete file {path}: {e}")

def validate_wav_file(file_path):
    """Check if the file is a valid WAV file"""
    try:
        with wave.open(file_path, 'rb') as wave_file:
            # Get basic info to validate the file
            channels = wave_file.getnchannels()
            sample_width = wave_file.getsampwidth() 
            frame_rate = wave_file.getframerate()
            frames = wave_file.getnframes()
            
            # Check for empty or corrupted files
            if frames == 0:
                app.logger.warning(f"WAV file {file_path} has 0 frames")
                return False
                
            # Log audio properties
            app.logger.info(f"WAV file validated: {file_path} - Channels: {channels}, "
                           f"Sample width: {sample_width}, Frame rate: {frame_rate}, "
                           f"Frames: {frames}")
            return True
    except Exception as e:
        app.logger.error(f"WAV validation error for {file_path}: {e}")
        return False

def ffmpeg_to_wav(input_path, output_path):
    """
    Convert any audio/video file to 16-bit 44.1kHz WAV stereo using FFmpeg.
    Returns True if conversion was successful, False otherwise.
    """
    try:
        # First, check if the input file exists and has content
        if not os.path.exists(input_path) or os.path.getsize(input_path) == 0:
            app.logger.error(f"Input file {input_path} doesn't exist or is empty")
            return False
            
        # Command with improved error handling and normalization
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vn",                    # No video
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",  # Normalize audio levels
            "-acodec", "pcm_s16le",   # 16-bit PCM
            "-ar", "44100",           # 44.1kHz sample rate
            "-ac", "2",               # Stereo (2 channels)
            "-f", "wav",              # Force WAV format
            output_path
        ]
        
        # Run FFmpeg with timeout
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        
        if proc.returncode != 0:
            stderr = proc.stderr.decode('utf-8')
            app.logger.error(f"FFmpeg error converting {input_path}:\n{stderr}")
            return False
            
        # Validate the output file
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
            app.logger.error(f"FFmpeg output file {output_path} missing or too small")
            return False
            
        # Validate WAV file structure
        if not validate_wav_file(output_path):
            app.logger.error(f"FFmpeg produced invalid WAV file: {output_path}")
            return False
            
        app.logger.info(f"Successfully converted {input_path} to {output_path}")
        return True
        
    except subprocess.TimeoutExpired:
        app.logger.error(f"FFmpeg timed out processing {input_path}")
        return False
    except Exception as e:
        app.logger.error(f"FFmpeg exception: {str(e)}")
        return False

def repair_wav_file(input_wav, output_wav):
    """
    Attempt to repair a corrupted WAV file using pydub.
    Returns True if successful, False otherwise.
    """
    try:
        app.logger.info(f"Attempting to repair WAV file: {input_wav}")
        # Try to load with pydub, which can be more forgiving than wave
        audio = AudioSegment.from_file(input_wav)
        audio.export(output_wav, format="wav")
        
        # Verify the repaired file
        if validate_wav_file(output_wav):
            app.logger.info(f"WAV repair successful: {output_wav}")
            return True
        else:
            app.logger.warning(f"WAV repair failed validation: {output_wav}")
            return False
    except Exception as e:
        app.logger.error(f"WAV repair error: {e}")
        return False

def produce_short_beep(out_path):
    """
    Creates a 1-second beep track as final fallback
    using pydub.generators.Sine for 440 Hz tone.
    """
    try:
        # Create a 1-second 440Hz sine wave
        beep = Sine(440).to_audio_segment(duration=1000)
        
        # Add fade in/out to avoid clicks
        beep = beep.fade_in(50).fade_out(50)
        
        # Export to WAV
        beep.export(out_path, format="wav")
        app.logger.info(f"Produced beep fallback at {out_path}")
        return True
    except Exception as e:
        app.logger.error(f"produce_short_beep error: {e}")
        return False

def simple_audio_processing(in_wav, out_wav):
    """
    Very basic audio processing:
    - Convert to mono if needed
    - Normalize to -3 dB
    Returns True if success, False if error
    """
    try:
        audio = AudioSegment.from_wav(in_wav)
        
        # Convert to mono if stereo
        if audio.channels > 1:
            audio = audio.set_channels(1)
            
        # Normalize
        normalized = audio.normalize()
        normalized.export(out_wav, format="wav")
        
        app.logger.info(f"Simple audio processing completed: {out_wav}")
        return True
    except Exception as e:
        app.logger.error(f"Simple audio processing error: {e}")
        return False

def basic_auto_master(in_wav, out_wav):
    """
    Basic audio mastering:
    - Normalize level
    - Basic compression
    Returns True if success, False if error
    """
    try:
        # Use FFmpeg for basic mastering (normalize + compress)
        cmd = [
            "ffmpeg", "-y",
            "-i", in_wav,
            "-af", "acompressor=threshold=-12dB:ratio=4:attack=200:release=1000:makeup=2dB,loudnorm=I=-14:TP=-1:LRA=11",
            out_wav
        ]
        
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        if proc.returncode != 0:
            app.logger.error(f"Basic mastering FFmpeg error: {proc.stderr.decode('utf-8')}")
            return False
            
        if validate_wav_file(out_wav):
            app.logger.info(f"Basic auto master completed: {out_wav}")
            return True
        return False
    except Exception as e:
        app.logger.error(f"Basic auto master error: {e}")
        return False

def final_auto_master_fallback(in_wav, out_wav):
    """
    Enhanced auto master:
    - High-pass 40Hz
    - normalize to 0 dB
    - +3 dB mild boost
    - final target ~ -12 dBFS
    Returns True if success, False if error
    """
    try:
        audio = AudioSegment.from_wav(in_wav)
        
        # Apply processing
        audio = audio.high_pass_filter(40)
        audio = audio.normalize()  # normalize to 0 dB
        audio = audio.apply_gain(3)  # mild push => ~ -3 dBFS
        
        # Final target = ~ -12 dBFS
        target_lufs = -12.0
        current_dBFS = audio.dBFS
        app.logger.info(f"Current dBFS: {current_dBFS}, Target LUFS: {target_lufs}")
        
        gain_needed = target_lufs - current_dBFS
        audio = audio.apply_gain(gain_needed)
        
        # Export
        audio.export(out_wav, format="wav")
        
        # Validate
        if validate_wav_file(out_wav):
            app.logger.info(f"Auto fallback master completed: {out_wav}")
            return True
        return False
    except Exception as e:
        app.logger.error(f"Auto fallback error: {e}")
        return False

def copy_input_as_fallback(in_wav, out_wav):
    """Simply copy the input file as a fallback when all else fails"""
    try:
        shutil.copy2(in_wav, out_wav)
        app.logger.info(f"Copied input as fallback: {out_wav}")
        return True
    except Exception as e:
        app.logger.error(f"Copy fallback error: {e}")
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
    # Recreate directories just to be sure they exist
    Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)
    Path(PROCESSED_FOLDER).mkdir(parents=True, exist_ok=True)
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    session_id = str(uuid.uuid4())
    app.logger.info(f"Starting new upload process, session ID: {session_id}")
    
    # Ensure directories exist
    Path(UPLOAD_FOLDER).mkdir(parents=True, exist_ok=True)
    Path(PROCESSED_FOLDER).mkdir(parents=True, exist_ok=True)
    
    # 1) Validate fields
    if "target_file" not in request.files or "reference_file" not in request.files:
        flash("Please upload both target and reference files.")
        return redirect(url_for("index"))
        
    target_file = request.files["target_file"]
    ref_file = request.files["reference_file"]
    
    if target_file.filename == "" or ref_file.filename == "":
        flash("No valid files selected.")
        return redirect(url_for("index"))
    
    # Log file info
    app.logger.info(f"Target file: {target_file.filename}, Reference file: {ref_file.filename}")
    
    # 2) Save them with safe filenames
    target_filename = "".join(c for c in target_file.filename if c.isalnum() or c in '._-')
    ref_filename = "".join(c for c in ref_file.filename if c.isalnum() or c in '._-')
    
    target_path = os.path.join(UPLOAD_FOLDER, f"{session_id}_target_{target_filename}")
    ref_path = os.path.join(UPLOAD_FOLDER, f"{session_id}_ref_{ref_filename}")

    app.logger.info(f"Saving target to: {target_path}")
    app.logger.info(f"Saving reference to: {ref_path}")
    
    target_file.save(target_path)
    ref_file.save(ref_path)
    
    # Check if files were saved correctly
    if not os.path.exists(target_path) or os.path.getsize(target_path) == 0:
        flash("Error saving target file. Please try again.")
        return redirect(url_for("index"))
        
    if not os.path.exists(ref_path) or os.path.getsize(ref_path) == 0:
        flash("Error saving reference file. Please try again.")
        return redirect(url_for("index"))
    
    # 3) Convert to WAV
    target_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_target.wav")
    ref_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_ref.wav")
    
    # Track conversion success
    target_conversion_ok = ffmpeg_to_wav(target_path, target_wav)
    ref_conversion_ok = ffmpeg_to_wav(ref_path, ref_wav)
    
    # If conversion failed, try to repair or use fallbacks
    if not target_conversion_ok:
        app.logger.warning(f"Target conversion failed, attempting repair")
        target_repair_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_target_repair.wav")
        target_conversion_ok = simple_audio_processing(target_path, target_repair_wav)
        if target_conversion_ok:
            target_wav = target_repair_wav
    
    if not ref_conversion_ok:
        app.logger.warning(f"Reference conversion failed, attempting repair")
        ref_repair_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_ref_repair.wav")
        ref_conversion_ok = simple_audio_processing(ref_path, ref_repair_wav)
        if ref_conversion_ok:
            ref_wav = ref_repair_wav
    
    # 4) Attempt AI Master if both conversions succeeded
    master_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_master.wav")
    ai_success = False
    fallback_method_used = "None"  # Track which fallback was used
    
    # Only attempt AI mastering if both files converted successfully
    if target_conversion_ok and ref_conversion_ok:
        try:
            app.logger.info("Starting Matchering AI mastering process")
            # Configure Matchering with increased timeout
            mg.configure(
                implementation=mg.HandlerbarsImpl(),
                result_bitrate=320,
                preview_size=30,
                # These two are important - increase tolerance and lower threshold
                threshold=-40,
                tolerance=0.1
            )
            
            # Process with Matchering
            mg.process(
                target=target_wav,
                reference=ref_wav,
                results=[mg.pcm16(master_wav)]
            )
            
            # Validate the result
            if os.path.exists(master_wav) and validate_wav_file(master_wav):
                ai_success = True
                fallback_method_used = "None"
                app.logger.info("AI master success!")
            else:
                app.logger.error("AI master produced invalid WAV file")
        except Exception as e:
            app.logger.error(f"Matchering error: {e}")
    else:
        app.logger.warning("Skipping AI mastering due to conversion failures")
    
    # 5) Fallback chain if AI fails
    if not ai_success:
        app.logger.info("AI mastering failed, trying fallback chain")
        
        # Try the original enhanced auto-master fallback
        enhanced_fallback_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_enhanced_fallback.wav")
        if target_conversion_ok and final_auto_master_fallback(target_wav, enhanced_fallback_wav):
            master_wav = enhanced_fallback_wav
            fallback_method_used = "Enhanced"
            app.logger.info("Enhanced auto fallback completed master.")
        
        # If that failed, try the basic auto master
        elif target_conversion_ok:
            basic_fallback_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_basic_fallback.wav")
            if basic_auto_master(target_wav, basic_fallback_wav):
                master_wav = basic_fallback_wav
                fallback_method_used = "Basic"
                app.logger.info("Basic auto fallback completed master.")
            
            # If that failed, just copy the input as a last resort before beep
            else:
                copy_fallback_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_copy_fallback.wav")
                if copy_input_as_fallback(target_wav, copy_fallback_wav):
                    master_wav = copy_fallback_wav
                    fallback_method_used = "Copy"
                    app.logger.info("Copy fallback used.")
                
                # Ultimate fallback: beep
                else:
                    beep_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_beep.wav")
                    produce_short_beep(beep_wav)
                    master_wav = beep_wav
                    fallback_method_used = "Beep"
                    app.logger.error("All fallbacks failed; beep fallback used.")
        
        # If target conversion also failed, go directly to beep
        else:
            beep_wav = os.path.join(PROCESSED_FOLDER, f"{session_id}_beep.wav")
            produce_short_beep(beep_wav)
            master_wav = beep_wav
            fallback_method_used = "Beep"
            app.logger.error("Target conversion and all fallbacks failed; beep fallback.")

    # 7) Convert to MP3 if requested
    export_format = request.form.get("export_format", "wav")
    final_output_path = master_wav
    
    if export_format == "mp3":
        mp3_path = os.path.join(PROCESSED_FOLDER, f"{session_id}_master.mp3")
        try:
            # Use FFmpeg with higher quality settings
            sp = subprocess.run([
                "ffmpeg", "-y",
                "-i", master_wav,
                "-codec:a", "libmp3lame", 
                "-qscale:a", "0",  # Best quality
                "-b:a", "320k",    # 320kbps bitrate
                mp3_path
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            
            if sp.returncode == 0 and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
                final_output_path = mp3_path
                app.logger.info(f"MP3 conversion successful: {mp3_path}")
            else:
                app.logger.error(f"MP3 conversion error: {sp.stderr.decode('utf-8')}")
                # keep WAV
        except Exception as e:
            app.logger.error(f"MP3 conversion exception: {e}")
            # keep WAV

    # 8) Rename final output with appropriate label
    if ai_success:
        label = "AI_Completed_Master"
    elif fallback_method_used == "Enhanced":
        label = "Enhanced_Auto_Master"
    elif fallback_method_used == "Basic":
        label = "Basic_Auto_Master"
    elif fallback_method_used == "Copy":
        label = "Original_Copy"
    elif fallback_method_used == "Beep":
        label = "BEEP_All_Methods_Failed"
    else:
        label = "Unknown_Method"

    ext = ".mp3" if final_output_path.endswith(".mp3") else ".wav"
    final_renamed = os.path.join(PROCESSED_FOLDER, f"{session_id}_{label}{ext}")
    
    try:
        os.rename(final_output_path, final_renamed)
        final_output_path = final_renamed
        app.logger.info(f"Final output renamed to: {final_renamed}")
    except Exception as e:
        app.logger.error(f"Rename final file error: {e}")

    # 9) Cleanup after response
    @after_this_request
    def cleanup_files(response):
        # Clean up all files except the final output
        for folder in [UPLOAD_FOLDER, PROCESSED_FOLDER]:
            for filename in os.listdir(folder):
                filepath = os.path.join(folder, filename)
                if filepath != final_output_path and session_id in filename:
                    cleanup_file(filepath)
        return response

    # 10) Return final file
    app.logger.info(f"Returning file to user: {final_output_path}")
    return send_file(final_output_path, as_attachment=True)

if __name__ == "__main__":
    # Configure logging
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level),
        format='%(asctime)s [%(levelname)s] %(message)s',
    )
    
    # Get port from environment variable (Heroku sets this)
    port = int(os.environ.get("PORT", 5000))
    
    # In production, don't use debug mode
    debug = os.environ.get("FLASK_ENV") == "development"
    
    app.run(host="0.0.0.0", port=port, debug=debug)