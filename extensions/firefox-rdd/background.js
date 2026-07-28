"use strict";

const MENU_LINK_ID = "rdd-send-link";
const MENU_SELECTION_ID = "rdd-send-selection";

async function getSettings() {
  const { rddBaseUrl = "" } = await browser.storage.local.get("rddBaseUrl");
  return { rddBaseUrl };
}

function normalizeBaseUrl(value) {
  return String(value || "").trim().replace(/\/+$/, "");
}

async function notify(title, message) {
  await browser.notifications.create({
    type: "basic",
    iconUrl: browser.runtime.getURL("icon.svg"),
    title,
    message,
  });
}

async function sendToRdd(textOrUrl) {
  const { rddBaseUrl } = await getSettings();
  const baseUrl = normalizeBaseUrl(rddBaseUrl);
  if (!baseUrl) {
    await notify("RDD Sender", "Configure your RDD URL in the extension options first.");
    await browser.runtime.openOptionsPage();
    return;
  }

  let response;
  try {
    response = await fetch(`${baseUrl}/api/submit`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ url: textOrUrl }),
    });
  } catch (_error) {
    await notify("RDD Sender", "Could not reach the configured RDD instance.");
    return;
  }

  if (!response.ok) {
    await notify("RDD Sender", `RDD returned HTTP ${response.status}.`);
    return;
  }

  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    await notify("RDD Sender", "RDD returned an invalid response.");
    return;
  }

  const message = typeof payload.message === "string" && payload.message
    ? payload.message
    : "Request submitted.";
  await notify(payload.ok ? "Sent to RDD" : "RDD could not submit", message);
}

browser.runtime.onInstalled.addListener(() => {
  browser.menus.create({
    id: MENU_LINK_ID,
    title: "Send link to RDD",
    contexts: ["link"],
  });
  browser.menus.create({
    id: MENU_SELECTION_ID,
    title: "Send selected URLs to RDD",
    contexts: ["selection"],
  });
});

browser.menus.onClicked.addListener((info) => {
  if (info.menuItemId === MENU_LINK_ID && info.linkUrl) {
    void sendToRdd(info.linkUrl);
    return;
  }
  if (info.menuItemId === MENU_SELECTION_ID && info.selectionText) {
    void sendToRdd(info.selectionText);
  }
});

browser.action.onClicked.addListener(() => {
  void browser.runtime.openOptionsPage();
});
