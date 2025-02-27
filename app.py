import os
import subprocess
import uuid
import logging
import time
import wave
import shutil
import json
from pathlib import Path
import threading

from flask import Flask, request, render_template, redirect, url_for, send_file, flash, jsonify, session
from pydub import AudioSegment
from pydub.generators import Sine
import matchering as mg
import redis

# Initialize Flask app
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "YOUR_SECRET_KEY")

# Configure Redis for job queue
redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
redis_client = redis.from_url(redis_url)

# Use directories that will work with Heroku's ephemeral filesystem
UPLOAD_FOLDER = os.path.join("/tmp", "uploads")
PROCESSED_FOLDER = os.path.join("/tmp", "processed")
LOG_FOLDER = os.path.join("/tmp", "logs")

# Ensure directories exist
for directory in [UPLOAD_FOLDER, PROCESSED_FOLDER, LOG_FOLDER]:
    Path(directory).mkdir(parents=True, exist_ok=True)

# Configure logging
file_handler = logging.FileHandler(os.path.join(LOG_FOLDER, 'app.log'))
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
app.logger.addHandler(file_handler)
app.logger.setLevel(logging.DEBUG)

############################################################
# JOB QUEUE MANAGEMENT
############################################################

class JobStatus:
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

def store_job_data(job_id, data):
    """Store job data in Redis"""
    redis_client.set(f"job:{job_id}", json.dumps(data))
    redis_client.expire(f"job:{job_id}", 3600)  # Expire after 1 hour

def get_job_data(job_id):
    """Get job data from Redis"""
    data = redis_client.get(f"job:{job_id}")
    if data:
        return json.loads(data)
    return None

def queue_job(job_id, target_path, ref_path, export_format):
    """Add a job to the processing queue"""
    job_data = {
        "id": job_id,
        "target_path": target_path,
        "ref_path": ref_path,
        "export_format": export_format,
        "status": JobStatus.PENDING,
        "created_at": time.time(),
        "result_path": None,
        "error": None
    }
    store_job_data(job_id, job_data)
    
    # Add to processing queue
    redis_client.lpush("audio_processing_queue", job_id)
    app.logger.info(f"Job {job_id} queued for processing")
    return job_id

def update_job_status(job_id, status, result_path=None, error=None):
    """Update job status in Redis"""
    job_data = get_job_data(job_id)
    if job_data:
        job_data["status"] = status
        if result_path:
            job_data["result_path"] = result_path
        if error:
            job_data["error"] = error
        job_data["updated_at"] = time.time()
        store_job_data(job_id, job_data)
        app.logger.info(f"Job {job_id} status updated to {status}")
        return True
    return False

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
    """Creates a 1-second beep track as final fallback"""
    try:
        # Create a 1-second 440Hz sine wave
        beep = Sine(440).to_audio_segment(duration=1000)
        beep = beep.fade_in(50).fade_out(50)
        beep.export(out_path, format="wav")
        app.logger.info(f"Produced beep fallback at {out_path}")
        return True
    except Exception as e:
        app.logger.error(f"produce_short_beep error: {e}")
        return False

def simple_audio_processing(in_wav, out_wav):
    """Very basic audio processing for fallback"""
    try:
        audio = AudioSegment.from_wav(in_wav)
        if audio.channels > 1:
            audio = audio.set_channels(1)
        normalized = audio.normalize()
        normalized.export(out_wav, format="wav")
        app.logger.info(f"Simple audio processing completed: {out_wav}")
        return True
    except Exception as e:
        app.logger.error(f"Simple audio processing error: {e}")
        return False

def basic_auto_master(in_wav, out_wav):
    """Basic audio mastering for fallback"""
    try:
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
    """Enhanced auto master fallback"""
    try:
        audio = AudioSegment.from_wav(in_wav)
        audio = audio.high_pass_filter(40)
        audio = audio.normalize()
        audio = audio.apply_gain(3)
        
        target_lufs = -12.0
        current_dBFS = audio.dBFS
        app.logger.info(f"Current dBFS: {current_dBFS}, Target LUFS: {target_lufs}")
        
        gain_needed = target_lufs - current_dBFS
        audio = audio.apply_gain(gain_needed)
        audio.export(out_wav, format="wav")
        
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

def convert_to_mp3(wav_path, mp3_path):
    """Convert WAV to MP3 using FFmpeg"""
    try:
        # Use FFmpeg with higher quality settings
        sp = subprocess.run([
            "ffmpeg", "-y",
            "-i", wav_path,
            "-codec:a", "libmp3lame", 
            "-qscale:a", "0",  # Best quality
            "-b:a", "320k",    # 320kbps bitrate
            mp3_path
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        
        if sp.returncode == 0 and os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0:
            app.logger.info(f"MP3 conversion successful: {mp3_path}")
            return True
        else:
            app.logger.error(f"MP3 conversion error: {sp.stderr.decode('utf-8')}")
            return False
    except Exception as e:
        app.logger.error(f"MP3 conversion exception: {e}")
        return False

############################################################
# AUDIO PROCESSING WORKER
############################################################

def process_audio(job_id, target_path, ref_path, export_format):
    """Process audio files and update job status"""
    app.logger.info(f"Starting processing for job {job_id}")
    update_job_status(job_id, JobStatus.PROCESSING)
    
    try:
        # 1. Convert inputs to WAV
        target_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_target.wav")
        ref_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_ref.wav")
        
        target_conversion_ok = ffmpeg_to_wav(target_path, target_wav)
        ref_conversion_ok = ffmpeg_to_wav(ref_path, ref_wav)
        
        # If conversion failed, try to repair
        if not target_conversion_ok:
            app.logger.warning(f"Target conversion failed, attempting repair")
            target_repair_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_target_repair.wav")
            target_conversion_ok = simple_audio_processing(target_path, target_repair_wav)
            if target_conversion_ok:
                target_wav = target_repair_wav
        
        if not ref_conversion_ok:
            app.logger.warning(f"Reference conversion failed, attempting repair")
            ref_repair_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_ref_repair.wav")
            ref_conversion_ok = simple_audio_processing(ref_path, ref_repair_wav)
            if ref_conversion_ok:
                ref_wav = ref_repair_wav
        
        # 2. Attempt AI mastering
        master_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_master.wav")
        ai_success = False
        fallback_method_used = "None"
        
        if target_conversion_ok and ref_conversion_ok:
            try:
                app.logger.info("Starting Matchering AI mastering process")
                # Configure Matchering with increased resilience
                mg.configure(
                    implementation=mg.HandlerbarsImpl(),
                    result_bitrate=320,
                    preview_size=30,
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
        
        # 3. Try fallbacks if AI fails
        if not ai_success:
            app.logger.info("AI mastering failed, trying fallback chain")
            
            # Try enhanced auto-master fallback
            enhanced_fallback_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_enhanced_fallback.wav")
            if target_conversion_ok and final_auto_master_fallback(target_wav, enhanced_fallback_wav):
                master_wav = enhanced_fallback_wav
                fallback_method_used = "Enhanced"
                app.logger.info("Enhanced auto fallback completed master.")
            
            # Try basic auto master
            elif target_conversion_ok:
                basic_fallback_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_basic_fallback.wav")
                if basic_auto_master(target_wav, basic_fallback_wav):
                    master_wav = basic_fallback_wav
                    fallback_method_used = "Basic"
                    app.logger.info("Basic auto fallback completed master.")
                
                # Try copy as fallback
                else:
                    copy_fallback_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_copy_fallback.wav")
                    if copy_input_as_fallback(target_wav, copy_fallback_wav):
                        master_wav = copy_fallback_wav
                        fallback_method_used = "Copy"
                        app.logger.info("Copy fallback used.")
                    
                    # Ultimate fallback: beep
                    else:
                        beep_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_beep.wav")
                        produce_short_beep(beep_wav)
                        master_wav = beep_wav
                        fallback_method_used = "Beep"
                        app.logger.error("All fallbacks failed; beep fallback used.")
            
            # If target conversion failed, use beep
            else:
                beep_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_beep.wav")
                produce_short_beep(beep_wav)
                master_wav = beep_wav
                fallback_method_used = "Beep"
                app.logger.error("Target conversion and all fallbacks failed; beep fallback.")
        
        # 4. Convert to MP3 if requested
        final_output_path = master_wav
        
        if export_format == "mp3":
            mp3_path = os.path.join(PROCESSED_FOLDER, f"{job_id}_master.mp3")
            if convert_to_mp3(master_wav, mp3_path):
                final_output_path = mp3_path
        
        # 5. Rename final output with appropriate label
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
        final_renamed = os.path.join(PROCESSED_FOLDER, f"{job_id}_{label}{ext}")
        
        try:
            os.rename(final_output_path, final_renamed)
            final_output_path = final_renamed
            app.logger.info(f"Final output renamed to: {final_renamed}")
        except Exception as e:
            app.logger.error(f"Rename final file error: {e}")
        
        # 6. Update job status to completed
        update_job_status(job_id, JobStatus.COMPLETED, final_output_path)
        
        # 7. Clean up temporary files
        for folder in [UPLOAD_FOLDER, PROCESSED_FOLDER]:
            for filename in os.listdir(folder):
                filepath = os.path.join(folder, filename)
                if filepath != final_output_path and job_id in filename:
                    cleanup_file(filepath)
        
    except Exception as e:
        app.logger.error(f"Error processing job {job_id}: {str(e)}")
        update_job_status(job_id, JobStatus.FAILED, error=str(e))

def worker_thread_function():
    """Background worker thread to process audio jobs"""
    app.logger.info("Starting background worker thread")
    while True:
        try:
            # Get the next job from the queue with a timeout
            result = redis_client.brpop("audio_processing_queue", timeout=10)
            if result is None:
                # No jobs available, sleep for a bit
                time.sleep(1)
                continue
                
            # Extract job ID from result
            _, job_id_bytes = result
            job_id = job_id_bytes.decode('utf-8')
            
            # Get job data
            job_data = get_job_data(job_id)
            if job_data is None:
                app.logger.error(f"No data found for job {job_id}")
                continue
                
            # Process the job
            app.logger.info(f"Processing job {job_id}")
            process_audio(
                job_id,
                job_data["target_path"],
                job_data["ref_path"],
                job_data["export_format"]
            )
            
        except Exception as e:
            app.logger.error(f"Worker thread error: {str(e)}")
            time.sleep(5)  # Sleep before retrying

# Start the worker thread when the app starts
@app.before_first_request
def start_worker_thread():
    thread = threading.Thread(target=worker_thread_function)
    thread.daemon = True  # Allow the thread to exit when the main process exits
    thread.start()
    app.logger.info("Worker thread started")

############################################################
# ROUTES
############################################################

@app.route("/")
def index():
    """Render the upload form"""
    # Recreate directories just to be sure they exist
    for directory in [UPLOAD_FOLDER, PROCESSED_FOLDER]:
        Path(directory).mkdir(parents=True, exist_ok=True)
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    """Handle file uploads and start processing"""
    # Generate a unique job ID
    job_id = str(uuid.uuid4())
    app.logger.info(f"Starting new upload process, session ID: {job_id}")
    
    # Ensure directories exist
    for directory in [UPLOAD_FOLDER, PROCESSED_FOLDER]:
        Path(directory).mkdir(parents=True, exist_ok=True)
    
    # 1. Validate fields
    if "target_file" not in request.files or "reference_file" not in request.files:
        flash("Please upload both target and reference files.")
        return redirect(url_for("index"))
        
    target_file = request.files["target_file"]
    ref_file = request.files["reference_file"]
    
    if target_file.filename == "" or ref_file.filename == "":
        flash("No valid files selected.")
        return redirect(url_for("index"))
    
    # 2. Save uploaded files
    target_filename = "".join(c for c in target_file.filename if c.isalnum() or c in '._-')
    ref_filename = "".join(c for c in ref_file.filename if c.isalnum() or c in '._-')
    
    target_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_target_{target_filename}")
    ref_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_ref_{ref_filename}")
    
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
    
    # 3. Add to processing queue
    export_format = request.form.get("export_format", "wav")
    queue_job(job_id, target_path, ref_path, export_format)
    
    # 4. Return job ID to client
    return redirect(url_for("status", job_id=job_id))

@app.route("/status/<job_id>")
def status(job_id):
    """Show job status and download link when ready"""
    job_data = get_job_data(job_id)
    if job_data is None:
        flash("Job not found. It may have expired.")
        return redirect(url_for("index"))
    
    return render_template("status.html", job=job_data)

@app.route("/api/status/<job_id>")
def api_status(job_id):
    """API endpoint for checking job status via AJAX"""
    job_data = get_job_data(job_id)
    if job_data is None:
        return jsonify({"error": "Job not found"}), 404
    
    return jsonify({
        "id": job_data["id"],
        "status": job_data["status"],
        "error": job_data.get("error")
    })

@app.route("/download/<job_id>")
def download(job_id):
    """Download the processed file"""
    job_data = get_job_data(job_id)
    if job_data is None or job_data["status"] != JobStatus.COMPLETED:
        flash("File not ready for download or job expired.")
        return redirect(url_for("index"))
    
    result_path = job_data["result_path"]
    if not os.path.exists(result_path):
        flash("The processed file was not found.")
        return redirect(url_for("index"))
    
    return send_file(result_path, as_attachment=True)

############################################################
# MAIN
############################################################

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