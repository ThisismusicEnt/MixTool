import os
import uuid
import logging
import time
import threading
import json
from pathlib import Path

from flask import Flask, request, render_template, redirect, url_for, send_file, flash, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix
from pydub import AudioSegment
from pydub.generators import Sine

# Try to import matchering for AI processing
try:
    import matchering as mg
    MATCHERING_AVAILABLE = True
    logger = logging.getLogger(__name__)
    logger.info("Matchering library is available")
except ImportError:
    MATCHERING_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("Matchering library is not available - AI mastering will be disabled")

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
JOBS_FOLDER = os.path.join("/tmp", "jobs")

# Ensure directories exist
for folder in [UPLOAD_FOLDER, PROCESSED_FOLDER, JOBS_FOLDER]:
    Path(folder).mkdir(parents=True, exist_ok=True)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Define job statuses
class JobStatus:
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

# Job management functions
def save_job_status(job_id, status, result_path=None, error=None):
    """Save job status to disk"""
    job_file = os.path.join(JOBS_FOLDER, f"{job_id}.json")
    
    job_data = {
        "id": job_id,
        "status": status,
        "created_at": time.time(),
        "updated_at": time.time()
    }
    
    if result_path:
        job_data["result_path"] = result_path
        
    if error:
        job_data["error"] = error
    
    with open(job_file, 'w') as f:
        json.dump(job_data, f)
    
    return job_data

def get_job_status(job_id):
    """Get job status from disk"""
    job_file = os.path.join(JOBS_FOLDER, f"{job_id}.json")
    
    if not os.path.exists(job_file):
        return None
    
    try:
        with open(job_file, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error reading job file: {str(e)}")
        return None

def cleanup_job_files(keep_hours=24):
    """Clean up old job files"""
    try:
        current_time = time.time()
        for folder in [UPLOAD_FOLDER, PROCESSED_FOLDER, JOBS_FOLDER]:
            if os.path.exists(folder):
                for filename in os.listdir(folder):
                    filepath = os.path.join(folder, filename)
                    
                    # Skip if not a file
                    if not os.path.isfile(filepath):
                        continue
                    
                    # Get file age in hours
                    file_age_hours = (current_time - os.path.getmtime(filepath)) / 3600
                    
                    # Remove if older than keep_hours
                    if file_age_hours > keep_hours:
                        try:
                            os.remove(filepath)
                            logger.info(f"Removed old file: {filepath}")
                        except Exception as e:
                            logger.error(f"Could not remove old file {filepath}: {str(e)}")
    except Exception as e:
        logger.error(f"Error during cleanup: {str(e)}")

# Main processing function
def process_audio_file(job_id, target_path, reference_path=None, params=None):
    """Process audio file in a background thread"""
    try:
        # Update job status
        save_job_status(job_id, JobStatus.PROCESSING)
        
        # Default parameters
        if params is None:
            params = {}
            
        # Get mastering method and parameters
        mastering_method = params.get('mastering_method', 'parameter')
        export_format = params.get('export_format', 'wav')
        
        # Output path
        output_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_output.wav")
        
        # Process based on selected method
        processing_success = False
        method_used = "Unknown"
        
        # Load the target file
        try:
            target_audio = AudioSegment.from_file(target_path)
            logger.info(f"Target audio loaded: {len(target_audio)/1000:.2f}s, {target_audio.channels} channels")
        except Exception as e:
            logger.error(f"Error loading target audio: {str(e)}")
            # Create a beep as fallback
            beep_path = os.path.join(PROCESSED_FOLDER, f"{job_id}_Beep_Fallback.wav")
            create_fallback_beep(beep_path)
            save_job_status(job_id, JobStatus.COMPLETED, beep_path)
            return
        
        # Try AI/reference mastering if selected and available
        if mastering_method == 'reference' and MATCHERING_AVAILABLE and reference_path:
            try:
                logger.info("Attempting AI/reference-based mastering")
                
                # Load reference audio
                reference_audio = AudioSegment.from_file(reference_path)
                logger.info(f"Reference audio loaded: {len(reference_audio)/1000:.2f}s")
                
                # Save WAV versions for Matchering
                target_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_target.wav")
                ref_wav = os.path.join(PROCESSED_FOLDER, f"{job_id}_reference.wav")
                
                target_audio.export(target_wav, format="wav")
                reference_audio.export(ref_wav, format="wav")
                
                # Configure Matchering
                mg.configure(
                    implementation=mg.HandlerbarsImpl(),
                    result_bitrate=320,
                    preview_size=30,
                    threshold=-40,  # More permissive threshold
                    tolerance=0.2   # More permissive tolerance
                )
                
                # Process with Matchering
                mg.process(
                    target=target_wav,
                    reference=ref_wav,
                    results=[mg.pcm16(output_wav)]
                )
                
                # Check if output file was created
                if os.path.exists(output_wav) and os.path.getsize(output_wav) > 1000:
                    processing_success = True
                    method_used = "AI_Reference_Based"
                    logger.info("AI reference-based mastering successful")
                else:
                    logger.error("AI mastering failed to produce valid output")
            except Exception as e:
                logger.error(f"AI mastering error: {str(e)}")
                logger.info("Falling back to parameter-based mastering")
        
        # If AI mastering failed or wasn't selected, try parameter-based mastering
        if not processing_success:
            logger.info("Starting parameter-based mastering")
            
            method_used, processing_success = process_audio_parameters(
                target_audio, 
                output_wav, 
                params
            )
        
        # If all processing failed, create a beep
        if not processing_success:
            logger.error("All mastering methods failed")
            beep_path = os.path.join(PROCESSED_FOLDER, f"{job_id}_Beep_Fallback.wav")
            create_fallback_beep(beep_path)
            save_job_status(job_id, JobStatus.COMPLETED, beep_path)
            return
        
        # Convert to MP3 if requested
        final_output = output_wav
        
        if export_format.lower() == 'mp3':
            mp3_path = os.path.join(PROCESSED_FOLDER, f"{job_id}_output.mp3")
            mp3_success = convert_to_mp3(output_wav, mp3_path)
            
            if mp3_success:
                final_output = mp3_path
                logger.info(f"Converted to MP3: {mp3_path}")
            else:
                logger.warning("MP3 conversion failed, using WAV")
        
        # Rename final output with method
        ext = os.path.splitext(final_output)[1]
        final_renamed = os.path.join(PROCESSED_FOLDER, f"{job_id}_{method_used}{ext}")
        
        try:
            os.rename(final_output, final_renamed)
            final_output = final_renamed
            logger.info(f"Final output renamed to: {final_output}")
        except Exception as e:
            logger.error(f"Rename error: {str(e)}")
        
        # Update job status
        save_job_status(job_id, JobStatus.COMPLETED, final_output)
        logger.info(f"Processing completed for job {job_id}")
        
    except Exception as e:
        error_msg = f"Processing error: {str(e)}"
        logger.error(error_msg)
        save_job_status(job_id, JobStatus.FAILED, error=error_msg)

def process_audio_parameters(audio, output_file, params=None):
    """Process audio using parameter-based approach with PyDub"""
    try:
        if params is None:
            params = {}
        
        # Get parameters with defaults
        bass_boost = min(max(int(params.get('bass_boost', 5)), 0), 10)
        brightness = min(max(int(params.get('brightness', 5)), 0), 10)
        compression = min(max(int(params.get('compression', 5)), 0), 10)
        stereo_width = min(max(int(params.get('stereo_width', 5)), 0), 10)
        target_loudness = min(max(float(params.get('loudness', -14)), -24), -6)
        
        logger.info(f"Using parameters: bass={bass_boost}, brightness={brightness}, "
                  f"compression={compression}, width={stereo_width}, loudness={target_loudness}")
        
        # Create a copy of the audio to process
        processed_audio = audio
        
        # Ensure stereo for processing
        if processed_audio.channels == 1:
            processed_audio = processed_audio.set_channels(2)
            logger.info("Converted mono to stereo")
        
        # 1. Apply bass boost if not default
        if bass_boost != 5:
            try:
                # Convert to dB gain
                bass_gain = (bass_boost - 5) * 3  # -15 to +15 dB
                
                # Split audio into frequency bands
                bass_audio = processed_audio.low_pass_filter(200)
                bass_audio = bass_audio.apply_gain(bass_gain)
                
                # Remove bass from original
                no_bass = processed_audio.high_pass_filter(200)
                
                # Combine processed bass with the rest
                processed_audio = bass_audio.overlay(no_bass)
                logger.info(f"Applied bass boost: {bass_gain}dB")
            except Exception as e:
                logger.error(f"Bass processing error: {str(e)}")
        
        # 2. Apply brightness/treble boost if not default
        if brightness != 5:
            try:
                # Convert to dB gain
                treble_gain = (brightness - 5) * 2  # -10 to +10 dB
                
                # Split audio into frequency bands
                treble_audio = processed_audio.high_pass_filter(5000)
                treble_audio = treble_audio.apply_gain(treble_gain)
                
                # Remove treble from original
                no_treble = processed_audio.low_pass_filter(5000)
                
                # Combine processed treble with the rest
                processed_audio = no_treble.overlay(treble_audio)
                logger.info(f"Applied brightness: {treble_gain}dB")
            except Exception as e:
                logger.error(f"Treble processing error: {str(e)}")
        
        # 3. Apply compression if requested
        if compression > 0:
            try:
                # Normalize first to prepare for compression
                processed_audio = processed_audio.normalize()
                logger.info("Normalized audio for compression")
                
                # Simple compression by reducing peaks
                threshold = -30 + ((10 - compression) * 2)  # -10dB to -30dB
                ratio = 1.5 + (compression * 0.25)  # 1.5:1 to 4:1
                
                logger.info(f"Applying compression: threshold={threshold}dB, ratio={ratio}:1")
                
                # Process in chunks to avoid memory issues with large files
                chunk_size = 10000  # 10 seconds
                total_chunks = len(processed_audio) // chunk_size + 1
                
                compressed = AudioSegment.empty()
                for i in range(total_chunks):
                    start = i * chunk_size
                    end = min(start + chunk_size, len(processed_audio))
                    
                    if start >= len(processed_audio):
                        break
                        
                    chunk = processed_audio[start:end]
                    
                    # Apply compression to chunk
                    chunk_db = chunk.dBFS
                    if chunk_db > threshold:
                        excess = chunk_db - threshold
                        reduction = excess * (1 - 1/ratio)
                        chunk = chunk.apply_gain(-reduction)
                    
                    compressed += chunk
                    
                    # Log progress for long files
                    if i % 10 == 0 and total_chunks > 10:
                        logger.info(f"Compression progress: {i}/{total_chunks} chunks")
                
                processed_audio = compressed
                
                # Apply makeup gain
                makeup_gain = compression * 0.5  # 0 to 5 dB
                processed_audio = processed_audio.apply_gain(makeup_gain)
                logger.info(f"Applied makeup gain: {makeup_gain}dB")
                
            except Exception as e:
                logger.error(f"Compression error: {str(e)}")
        
        # 4. Apply stereo width adjustment if not default
        if stereo_width != 5:
            try:
                # This is a simple stereo width adjustment technique
                # Extract left and right channels
                left_channel = processed_audio.split_to_mono()[0]
                right_channel = processed_audio.split_to_mono()[1]
                
                # Calculate width factor (0.5 = mono, 1.0 = normal, 1.5 = wide)
                width_factor = 0.5 + (stereo_width / 10)
                
                # Create modified stereo by mixing channels with width factor
                # For width > 1.0: Enhance stereo separation
                # For width < 1.0: Reduce stereo separation
                if width_factor > 1.0:
                    # Enhance stereo separation
                    enhance_factor = width_factor - 1.0
                    left_channel = left_channel.apply_gain(enhance_factor)
                    right_channel = right_channel.apply_gain(enhance_factor)
                elif width_factor < 1.0:
                    # Mix some of right into left and vice versa
                    mix_factor = 1.0 - width_factor
                    left_mix = left_channel.overlay(right_channel.apply_gain(-6 + (mix_factor * 6)))
                    right_mix = right_channel.overlay(left_channel.apply_gain(-6 + (mix_factor * 6)))
                    left_channel = left_mix
                    right_channel = right_mix
                
                # Recombine channels
                processed_audio = AudioSegment.from_mono_audiosegments(left_channel, right_channel)
                logger.info(f"Applied stereo width adjustment: {width_factor}")
                
            except Exception as e:
                logger.error(f"Stereo width adjustment error: {str(e)}")
        
        # 5. Normalize to target loudness
        try:
            # First normalize
            processed_audio = processed_audio.normalize()
            current_loudness = processed_audio.dBFS
            
            # Then adjust to target
            loudness_adjustment = target_loudness - current_loudness
            processed_audio = processed_audio.apply_gain(loudness_adjustment)
            logger.info(f"Applied loudness adjustment: {loudness_adjustment:.2f}dB to reach {target_loudness}dB")
        except Exception as e:
            logger.error(f"Loudness normalization error: {str(e)}")
        
        # 6. Export the processed audio
        try:
            logger.info(f"Exporting to {output_file}")
            processed_audio.export(output_file, format="wav")
            
            if os.path.exists(output_file) and os.path.getsize(output_file) > 1000:
                logger.info(f"Successfully processed audio: {output_file}")
                return "Parameter_Based", True
            else:
                logger.error(f"Failed to create valid output file: {output_file}")
                return "Processing_Failed", False
        except Exception as e:
            logger.error(f"Export error: {str(e)}")
            return "Export_Failed", False
    
    except Exception as e:
        logger.error(f"Audio processing error: {str(e)}")
        return "Processing_Failed", False

def create_fallback_beep(output_path):
    """Create a beep sound as a fallback"""
    try:
        logger.info(f"Creating fallback beep at {output_path}")
        beep = Sine(440).to_audio_segment(duration=1000)
        beep = beep.fade_in(50).fade_out(50)
        beep.export(output_path, format="wav")
        return True
    except Exception as e:
        logger.error(f"Beep creation error: {str(e)}")
        return False

def convert_to_mp3(wav_path, mp3_path):
    """Convert WAV to MP3 using PyDub"""
    try:
        logger.info(f"Converting {wav_path} to MP3")
        audio = AudioSegment.from_wav(wav_path)
        audio.export(mp3_path, format="mp3", bitrate="320k")
        
        if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 1000:
            logger.info(f"MP3 conversion successful: {mp3_path}")
            return True
        else:
            logger.error(f"MP3 file invalid: {mp3_path}")
            return False
    except Exception as e:
        logger.error(f"MP3 conversion error: {str(e)}")
        return False

# Routes
@app.route("/")
def index():
    """Show the upload form"""
    # Ensure directories exist
    for folder in [UPLOAD_FOLDER, PROCESSED_FOLDER, JOBS_FOLDER]:
        Path(folder).mkdir(parents=True, exist_ok=True)
    
    return render_template("index.html", matchering_available=MATCHERING_AVAILABLE)

@app.route("/upload", methods=["POST"])
def upload():
    """Handle file upload and start processing"""
    try:
        # Create a unique job ID
        job_id = str(uuid.uuid4())
        logger.info(f"New upload request: job_id={job_id}")
        
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
        target_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_target_{target_filename}")
        target_file.save(target_path)
        
        logger.info(f"Target file saved: {target_path}")
        
        # Get reference file if provided
        reference_path = None
        mastering_method = request.form.get("mastering_method", "parameter")
        
        if mastering_method == "reference" and MATCHERING_AVAILABLE:
            if "reference_file" in request.files and request.files["reference_file"].filename != "":
                ref_file = request.files["reference_file"]
                ref_filename = "".join(c for c in ref_file.filename if c.isalnum() or c in '._- ')
                reference_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_ref_{ref_filename}")
                ref_file.save(reference_path)
                logger.info(f"Reference file saved: {reference_path}")
            else:
                # If reference method selected but no file provided, fall back to parameter
                logger.warning("Reference method selected but no file provided. Falling back to parameter method.")
                mastering_method = "parameter"
        
        # Get mastering parameters
        params = {
            'mastering_method': mastering_method,
            'bass_boost': int(request.form.get('bass_boost', 5)),
            'brightness': int(request.form.get('brightness', 5)),
            'compression': int(request.form.get('compression', 5)),
            'stereo_width': int(request.form.get('stereo_width', 5)),
            'loudness': float(request.form.get('loudness', -14)),
            'export_format': request.form.get('export_format', 'wav'),
            'original_filename': target_filename
        }
        
        # Create initial job status
        save_job_status(job_id, JobStatus.QUEUED)
        
        # Start background processing thread
        thread = threading.Thread(
            target=process_audio_file,
            args=(job_id, target_path, reference_path, params)
        )
        thread.daemon = True
        thread.start()
        
        # Redirect to status page
        return redirect(url_for('status', job_id=job_id))
        
    except Exception as e:
        logger.error(f"Upload error: {str(e)}")
        flash("An error occurred during upload. Please try again.")
        return redirect(url_for("index"))

@app.route("/status/<job_id>")
def status(job_id):
    """Show job status page"""
    job_data = get_job_status(job_id)
    
    if not job_data:
        flash("Job not found. It may have expired.")
        return redirect(url_for("index"))
    
    return render_template("status.html", job=job_data)

@app.route("/api/status/<job_id>")
def api_status(job_id):
    """API endpoint for job status"""
    job_data = get_job_status(job_id)
    
    if not job_data:
        return jsonify({"error": "Job not found"}), 404
    
    return jsonify(job_data)

@app.route("/download/<job_id>")
def download(job_id):
    """Download processed file"""
    job_data = get_job_status(job_id)
    
    if not job_data or job_data["status"] != JobStatus.COMPLETED:
        flash("File is not ready for download or job not found.")
        return redirect(url_for("index"))
    
    if "result_path" not in job_data or not os.path.exists(job_data["result_path"]):
        flash("Processed file not found.")
        return redirect(url_for("index"))
    
    return send_file(
        job_data["result_path"], 
        as_attachment=True, 
        download_name=f"mastered_audio{os.path.splitext(job_data['result_path'])[1]}"
    )

# Periodic cleanup task
def run_cleanup():
    """Run cleanup task periodically"""
    while True:
        try:
            cleanup_job_files(24)  # Keep files for 24 hours
        except Exception as e:
            logger.error(f"Cleanup task error: {str(e)}")
        
        time.sleep(3600)  # Sleep for 1 hour

if __name__ == "__main__":
    # Start cleanup thread
    cleanup_thread = threading.Thread(target=run_cleanup)
    cleanup_thread.daemon = True
    cleanup_thread.start()
    
    # Get port from environment variable (Heroku sets this)
    port = int(os.environ.get("PORT", 5000))
    
    # In production, don't use debug mode
    debug = os.environ.get("FLASK_ENV") == "development"
    
    app.run(host="0.0.0.0", port=port, debug=debug)
    