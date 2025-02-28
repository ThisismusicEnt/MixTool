# MixTool - Audio Mastering Application

MixTool is a web-based audio mastering application that provides professional-quality masters using both parameter-based and reference-based approaches. The application is built with Flask and uses various audio processing libraries to deliver high-quality results.

## Features

- **Parameter-Based Mastering**: Customize your master with intuitive controls for bass, brightness, compression, stereo width, and loudness
- **Reference-Based Mastering**: Match the sound of your track to a professional reference track (requires matchering library)
- **Multiple Fallback Methods**: Ensures you always get a processed file even if primary mastering methods fail
- **Support for Common Audio Formats**: Process WAV, MP3, AIFF, FLAC, and other popular formats
- **High-Quality Export**: Download your masters in WAV or MP3 format

## Project Structure

```
MixTool/
├── static/
│   ├── css/
│   │   └── style.css
│   ├── js/
│   │   └── main.js
│   └── Logo.png
├── templates/
│   ├── base.html
│   ├── download.html
│   ├── index.html
│   └── status.html
├── app.py
├── cleanup.py
├── requirements.txt
└── README.md
```

## Installation

1. Clone this repository:
   ```
   git clone https://github.com/yourusername/mixtool.git
   cd mixtool
   ```

2. Create a virtual environment and activate it:
   ```
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install required packages:
   ```
   pip install -r requirements.txt
   ```

4. Install FFmpeg:
   - **Ubuntu/Debian**: `sudo apt-get install ffmpeg`
   - **macOS**: `brew install ffmpeg`
   - **Windows**: Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH

## Usage

1. Start the application:
   ```
   python app.py
   ```

2. Open your browser and navigate to:
   ```
   http://localhost:5000
   ```

3. Upload your track, choose mastering settings, and process your audio.

## Deploying to Heroku

This application is designed to be easily deployed to Heroku:

1. Create a Heroku account and install the Heroku CLI
2. Create a new Heroku app:
   ```
   heroku create your-mixtool-app-name
   ```

3. Add the FFmpeg buildpack:
   ```
   heroku buildpacks:add https://github.com/jonathanong/heroku-buildpack-ffmpeg-latest.git
   ```

4. Deploy the application:
   ```
   git push heroku main
   ```

## Environment Variables

The following environment variables can be configured:

- `SECRET_KEY`: Flask session encryption key (default: "development_secret_key")
- `PORT`: Server port (default: 5000)
- `FLASK_ENV`: Set to "development" for debug mode
- `AUDIO_STORAGE_PATH`: Path for storing audio files (default: "/tmp")

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgements

- [FFmpeg](https://ffmpeg.org/) for audio conversion and processing
- [PyDub](https://github.com/jiaaro/pydub) for Python audio processing
- [Matchering](https://github.com/sergree/matchering) for reference-based mastering
- [Flask](https://flask.palletsprojects.com/) for the web framework