// main.js
function pollProgress(sessionId) {
    const progressContainer = document.getElementById("progressContainer");
    if (!progressContainer) return;
  
    let intervalId = setInterval(() => {
      fetch(`/progress/${sessionId}`)
        .then(res => res.json())
        .then(data => {
          let prog = data.progress;
          // Create or update a progress bar
          let bar = document.getElementById("progressBar");
          if (!bar) {
            bar = document.createElement("div");
            bar.id = "progressBar";
            bar.style.height = "20px";
            bar.style.background = "green";
            bar.style.width = "0%";
            progressContainer.appendChild(bar);
          }
          bar.style.width = prog + "%";
  
          if (prog >= 100) {
            clearInterval(intervalId);
          }
        })
        .catch(err => console.log(err));
    }, 2000);
  }
  
  // You'd call pollProgress(sessionId) after upload submission, or after a redirect, etc.
  