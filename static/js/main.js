/**
 * MixTool - Main JavaScript
 */

document.addEventListener('DOMContentLoaded', function() {
    // Initialize tooltips
    const tooltips = document.querySelectorAll('[data-bs-toggle="tooltip"]');
    if (tooltips.length > 0) {
        tooltips.forEach(tooltip => {
            new bootstrap.Tooltip(tooltip);
        });
    }
    
    // Form validation
    const masteringForm = document.getElementById('masteringForm');
    if (masteringForm) {
        masteringForm.addEventListener('submit', function(event) {
            if (!validateForm()) {
                event.preventDefault();
                event.stopPropagation();
            } else {
                // Show loading state
                document.getElementById('submitButton').disabled = true;
                
                if (document.getElementById('loading-indicator')) {
                    document.getElementById('loading-indicator').style.display = 'block';
                }
            }
        });
    }
    
    // Mastering method selection
    const parameterOption = document.getElementById('parameterOption');
    const referenceOption = document.getElementById('referenceOption');
    
    if (parameterOption && referenceOption) {
        parameterOption.addEventListener('click', function() {
            selectMasteringMethod('parameter');
        });
        
        referenceOption.addEventListener('click', function() {
            selectMasteringMethod('reference');
        });
    }
    
    // File input custom validation
    const targetFileInput = document.getElementById('targetFile');
    const referenceFileInput = document.getElementById('referenceFile');
    
    if (targetFileInput) {
        targetFileInput.addEventListener('change', function() {
            validateFileInput(this);
        });
    }
    
    if (referenceFileInput) {
        referenceFileInput.addEventListener('change', function() {
            validateFileInput(this);
        });
    }
    
    // Update parameter displays
    updateRangeDisplays();
});

/**
 * Validates file inputs and shows appropriate messages
 */
function validateFileInput(input) {
    const fileExtensions = ['.mp3', '.wav', '.aif', '.aiff', '.flac', '.ogg', '.m4a'];
    const maxFileSize = 500 * 1024 * 1024; // 500MB
    
    if (input.files.length > 0) {
        const file = input.files[0];
        const fileName = file.name.toLowerCase();
        const fileSize = file.size;
        
        // Check file extension
        const validExtension = fileExtensions.some(ext => fileName.endsWith(ext));
        
        if (!validExtension) {
            showError(input, `Please select a valid audio file (${fileExtensions.join(', ')})`);
            input.value = ''; // Clear the input
            return false;
        }
        
        // Check file size
        if (fileSize > maxFileSize) {
            showError(input, `File size exceeds the maximum allowed (500MB)`);
            input.value = ''; // Clear the input
            return false;
        }
        
        // Valid file
        clearError(input);
        return true;
    }
    
    return true; // No file selected is valid (will be caught by required attribute if needed)
}

/**
 * Shows error message for an input
 */
function showError(input, message) {
    // Clear any existing error
    clearError(input);
    
    // Create error message
    const errorDiv = document.createElement('div');
    errorDiv.className = 'invalid-feedback d-block';
    errorDiv.textContent = message;
    
    // Add error styling
    input.classList.add('is-invalid');
    
    // Append error after input
    input.parentNode.appendChild(errorDiv);
}

/**
 * Clears error message for an input
 */
function clearError(input) {
    input.classList.remove('is-invalid');
    
    // Remove any existing error messages
    const existingError = input.parentNode.querySelector('.invalid-feedback');
    if (existingError) {
        existingError.remove();
    }
}

/**
 * Validates the entire form before submission
 */
function validateForm() {
    const form = document.getElementById('masteringForm');
    let isValid = true;
    
    // Check target file
    const targetFile = document.getElementById('targetFile');
    if (targetFile && !targetFile.files.length) {
        showError(targetFile, 'Please select an audio file to master');
        isValid = false;
    }
    
    // Check reference file if reference method is selected
    const referenceMethod = document.querySelector('input[name="mastering_method"]:checked').value === 'reference';
    const referenceFile = document.getElementById('referenceFile');
    
    if (referenceMethod && referenceFile && !referenceFile.files.length) {
        showError(referenceFile, 'Please select a reference track or switch to parameter-based mastering');
        isValid = false;
    }
    
    return isValid;
}

/**
 * Switches between parameter and reference-based mastering methods
 */
function selectMasteringMethod(method) {
    const parameterOption = document.getElementById('parameterOption');
    const referenceOption = document.getElementById('referenceOption');
    const parameterRadio = document.getElementById('parameterRadio');
    const referenceRadio = document.getElementById('referenceRadio');
    const referenceUpload = document.getElementById('referenceUpload');
    const parameterControls = document.getElementById('parameterControls');
    
    if (method === 'parameter') {
        parameterOption.classList.add('active');
        referenceOption.classList.remove('active');
        parameterRadio.checked = true;
        
        if (referenceUpload) {
            referenceUpload.style.display = 'none';
        }
        
        if (parameterControls) {
            parameterControls.style.display = 'block';
        }
    } else {
        parameterOption.classList.remove('active');
        referenceOption.classList.add('active');
        referenceRadio.checked = true;
        
        if (referenceUpload) {
            referenceUpload.style.display = 'block';
        }
        
        if (parameterControls) {
            parameterControls.style.display = 'block'; // Still show parameters for fine-tuning
        }
    }
}

/**
 * Updates displays for range inputs
 */
function updateRangeDisplays() {
    // Get all range inputs with value displays
    const rangeInputs = [
        { input: document.getElementById('loudness'), display: document.getElementById('loudnessValue'), suffix: ' LUFS' }
    ];
    
    for (const item of rangeInputs) {
        if (item.input && item.display) {
            // Set initial value
            item.display.textContent = item.input.value + (item.suffix || '');
            
            // Update on input
            item.input.addEventListener('input', function() {
                item.display.textContent = this.value + (item.suffix || '');
            });
        }
    }
}