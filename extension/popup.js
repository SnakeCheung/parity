"use strict";

const BRIDGE_URL = "http://127.0.0.1:8765/v1/collect";
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,}$/;

const tokenInput = document.getElementById("token");
const saveButton = document.getElementById("save");
const collectButton = document.getElementById("collect");
const statusBox = document.getElementById("status");

function setStatus(message, kind = "idle") {
  statusBox.textContent = message;
  statusBox.className = `status ${kind}`;
}

async function saveToken() {
  const token = tokenInput.value.trim();
  if (!TOKEN_PATTERN.test(token)) {
    throw new Error("配对令牌格式不正确");
  }
  await chrome.storage.local.set({ parityBridgeToken: token });
  return token;
}

function extractJdSearchPage() {
  const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const firstText = (root, selectors) => {
    for (const selector of selectors) {
      const node = root.querySelector(selector);
      const text = normalize(node?.textContent);
      if (text) return text;
    }
    return null;
  };
  const firstLink = (root) => {
    const selectors = [
      ".p-name a[href]",
      "a[href*='//item.jd.com/']",
      "a[href*='//item.m.jd.com/']"
    ];
    for (const selector of selectors) {
      const node = root.querySelector(selector);
      if (!node?.href) continue;
      try {
        const url = new URL(node.href, location.href);
        if (["item.jd.com", "item.m.jd.com"].includes(url.hostname)) {
          return url.href;
        }
      } catch (_) {
        // Ignore malformed product links.
      }
    }
    return null;
  };

  if (location.hostname !== "search.jd.com") {
    throw new Error("当前标签页不是京东搜索结果页");
  }

  const roots = Array.from(
    document.querySelectorAll(
      "#J_goodsList li.gl-item, #J_goodsList [data-sku], " +
      ".gl-warp > li[data-sku], li.gl-item[data-sku], li[data-sku]"
    )
  );
  const items = [];
  const seen = new Set();

  for (const root of roots) {
    const productUrl = firstLink(root);
    const urlSku = productUrl?.match(/\/(\d+)\.html(?:$|[?#])/)?.[1] || null;
    const rawSku = normalize(root.dataset?.sku || root.getAttribute("data-sku"));
    const sku = /^\d{5,30}$/.test(rawSku) ? rawSku : urlSku;
    const title = firstText(root, [
      ".p-name em",
      ".p-name",
      "[class*='p-name']",
      "a[href*='//item.jd.com/']"
    ]);
    const identity = sku || productUrl;
    if (!identity || !title || seen.has(identity)) continue;
    seen.add(identity);

    const priceText = firstText(root, [
      ".p-price i",
      ".p-price strong",
      "[class*='price'] i",
      "[class*='price']"
    ]);
    const priceMatch = normalize(priceText).replace(/,/g, "").match(/\d+(?:\.\d{1,2})?/);
    items.push({
      position: items.length + 1,
      sku,
      title,
      price: priceMatch?.[0] || null,
      store_name: firstText(root, [".p-shop a", ".p-shop", "[class*='shop'] a"]),
      product_url: productUrl
    });
    if (items.length >= 200) break;
  }

  const params = new URLSearchParams(location.search);
  const query = normalize(
    params.get("keyword") ||
    document.querySelector("#key")?.value ||
    document.querySelector("input[name='keyword']")?.value
  );
  return {
    schema_version: 1,
    site: "jd_search",
    page_url: location.href,
    page_title: document.title,
    query,
    collected_at: new Date().toISOString(),
    items
  };
}

async function collectCurrentPage() {
  collectButton.disabled = true;
  setStatus("正在读取当前页面…", "working");
  try {
    const token = await saveToken();
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !tab.url?.startsWith("https://search.jd.com/")) {
      throw new Error("请先打开京东搜索结果页，再点击扩展");
    }
    const execution = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: extractJdSearchPage
    });
    const collection = execution?.[0]?.result;
    if (!collection) {
      throw new Error("没有从当前页面读取到结果");
    }
    const response = await fetch(BRIDGE_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Parity-Token": token
      },
      body: JSON.stringify(collection)
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(result.reason || `本机接收服务返回 ${response.status}`);
    }
    if (result.item_count === 0) {
      setStatus("页面已提交，但没有识别到商品；请先滚动页面让商品加载完成。", "error");
      return;
    }
    setStatus(`采集成功：已保存 ${result.item_count} 个商品`, "success");
  } catch (error) {
    setStatus(error?.message || "采集失败", "error");
  } finally {
    collectButton.disabled = false;
  }
}

async function initialize() {
  try {
    if (chrome.storage.local.setAccessLevel) {
      await chrome.storage.local.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
    }
  } catch (_) {
    // Older supported builds may not expose access-level control.
  }
  const stored = await chrome.storage.local.get(["parityBridgeToken"]);
  tokenInput.value = stored.parityBridgeToken || "";
}

saveButton.addEventListener("click", async () => {
  try {
    await saveToken();
    setStatus("配对令牌已保存在本机扩展中", "success");
  } catch (error) {
    setStatus(error?.message || "保存失败", "error");
  }
});
collectButton.addEventListener("click", collectCurrentPage);
initialize();
