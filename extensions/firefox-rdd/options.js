"use strict";

const form = document.getElementById("options-form");
const baseUrlInput = document.getElementById("rdd-base-url");
const testButton = document.getElementById("test-connection");
const status = document.getElementById("status");

function normalizeBaseUrl(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

function originPattern(baseUrl) {
  const parsed = new URL(baseUrl);
  return `${parsed.protocol}//${parsed.host}/*`;
}

function setStatus(message) {
  status.textContent = message;
}

async function requestHostPermission(baseUrl) {
  return browser.permissions.request({
    origins: [originPattern(baseUrl)],
  });
}

async function saveOptions() {
  const baseUrl = normalizeBaseUrl(baseUrlInput.value);
  if (!baseUrl) {
    setStatus("Enter an RDD base URL.");
    return false;
  }

  let permitted = false;
  try {
    permitted = await requestHostPermission(baseUrl);
  } catch (_error) {
    setStatus("The RDD URL is not valid.");
    return false;
  }

  if (!permitted) {
    setStatus("Firefox permission was not granted for this RDD URL.");
    return false;
  }

  await browser.storage.local.set({ rddBaseUrl: baseUrl });
  baseUrlInput.value = baseUrl;
  setStatus("Saved.");
  return true;
}

async function testConnection() {
  const baseUrl = normalizeBaseUrl(baseUrlInput.value);
  if (!baseUrl) {
    setStatus("Enter an RDD base URL before testing.");
    return;
  }

  const saved = await saveOptions();
  if (!saved) {
    return;
  }

  let response;
  try {
    response = await fetch(`${baseUrl}/api/version`);
  } catch (_error) {
    setStatus("Could not reach RDD.");
    return;
  }

  if (!response.ok) {
    setStatus(`RDD returned HTTP ${response.status}.`);
    return;
  }

  try {
    const payload = await response.json();
    if (payload && payload.name && payload.version) {
      setStatus(`Connected to ${payload.name} v${payload.version}.`);
      return;
    }
  } catch (_error) {
    // Fall through to generic invalid response status.
  }

  setStatus("RDD returned an invalid version response.");
}

async function restoreOptions() {
  const { rddBaseUrl = "" } = await browser.storage.local.get("rddBaseUrl");
  baseUrlInput.value = rddBaseUrl;
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  void saveOptions();
});

testButton.addEventListener("click", () => {
  void testConnection();
});

void restoreOptions();
