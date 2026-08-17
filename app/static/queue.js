(() => {
  const container = document.getElementById("queue-sections");
  const status = document.getElementById("queue-live-status");
  if (!container || !status) {
    return;
  }

  let draggedItem = null;
  let pendingHtml = null;
  let isSubmittingMove = false;

  const reorderableItems = () => Array.from(
    container.querySelectorAll(".queue-item[data-can-reorder='true']"),
  );

  const targetPositionFor = (item) => reorderableItems().indexOf(item);

  const moveItem = async (item) => {
    const gid = item.dataset.queueGid;
    const position = targetPositionFor(item);
    if (!gid || position < 0) {
      return;
    }

    isSubmittingMove = true;
    status.textContent = "Saving queue order...";
    try {
      const response = await fetch(`/api/queue/${encodeURIComponent(gid)}/move`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ position }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) {
        throw new Error("Queue reorder failed");
      }
      status.textContent = "Queue order saved";
      pendingHtml = null;
    } catch (_error) {
      status.textContent = "Queue reorder failed";
      if (pendingHtml) {
        container.innerHTML = pendingHtml;
        pendingHtml = null;
      }
    } finally {
      isSubmittingMove = false;
    }
  };

  const nearestDragTarget = (clientY) => {
    const siblings = reorderableItems().filter((item) => item !== draggedItem);
    return siblings.reduce((nearest, item) => {
      const box = item.getBoundingClientRect();
      const offset = clientY - box.top - box.height / 2;
      if (offset < 0 && offset > nearest.offset) {
        return { offset, item };
      }
      return nearest;
    }, { offset: Number.NEGATIVE_INFINITY, item: null }).item;
  };

  container.addEventListener("dragstart", (event) => {
    const item = event.target.closest(".queue-item[data-can-reorder='true']");
    if (!item) {
      return;
    }
    draggedItem = item;
    item.classList.add("dragging");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", item.dataset.queueGid || "");
    status.textContent = "Reordering queue...";
  });

  container.addEventListener("dragover", (event) => {
    if (!draggedItem) {
      return;
    }
    event.preventDefault();
    const target = nearestDragTarget(event.clientY);
    if (target) {
      target.before(draggedItem);
    } else {
      const items = reorderableItems();
      const last = items[items.length - 1];
      if (last && last !== draggedItem) {
        last.after(draggedItem);
      }
    }
  });

  container.addEventListener("drop", (event) => {
    if (!draggedItem) {
      return;
    }
    event.preventDefault();
    const item = draggedItem;
    draggedItem = null;
    item.classList.remove("dragging");
    void moveItem(item);
  });

  container.addEventListener("dragend", () => {
    if (draggedItem) {
      draggedItem.classList.remove("dragging");
      draggedItem = null;
      if (pendingHtml && !isSubmittingMove) {
        container.innerHTML = pendingHtml;
        pendingHtml = null;
      }
    }
  });

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
        if (draggedItem || isSubmittingMove) {
          pendingHtml = payload.html;
          return;
        }
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
