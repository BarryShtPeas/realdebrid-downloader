(() => {
  const container = document.getElementById("queue-sections");
  const status = document.getElementById("queue-live-status");
  if (!container || !status) {
    return;
  }
  if (!("EventSource" in window)) {
    status.textContent = "Live updates unavailable";
    return;
  }

  const source = new EventSource("/queue/events");
  source.addEventListener("open", () => {
    status.textContent = "Live updates active";
  });
  source.addEventListener("queue", (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (typeof payload.html === "string") {
        container.innerHTML = payload.html;
        status.textContent = "Live updates active";
      }
    } catch (_error) {
      status.textContent = "Live updates unavailable";
    }
  });
  source.addEventListener("error", (event) => {
    let message = "Live updates reconnecting...";
    if ("data" in event && event.data) {
      try {
        const payload = JSON.parse(event.data);
        if (typeof payload.message === "string") {
          message = payload.message;
        }
      } catch (_error) {
        message = "Live updates unavailable";
      }
    }
    status.textContent = source.readyState === EventSource.CLOSED
      ? "Live updates unavailable"
      : message;
  });
})();
