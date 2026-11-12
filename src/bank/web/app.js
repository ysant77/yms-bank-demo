// YMS Bank demo front end: hash routes, fetch calls, no build step.
"use strict";

const $ = (selector) => document.querySelector(selector);
let customer = null;
let accounts = [];

function errorText(body, fallback) {
  const detail = body && body.detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg).join("; ");
  return detail || fallback;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(errorText(body, response.statusText));
    error.status = response.status;
    throw error;
  }
  return body;
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function showError(message) {
  const box = $("#error");
  box.textContent = message || "";
  box.hidden = !message;
}

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function show(view) {
  document.querySelectorAll(".view").forEach((section) => {
    section.hidden = section.id !== `view-${view}`;
  });
  $("#nav").hidden = !customer;
  $("#who").textContent = customer ? customer.name : "";
}

async function loadSession() {
  try {
    customer = await api("/api/session");
  } catch (error) {
    if (error.status !== 401) throw error;
    customer = null;
  }
}

async function renderChoose() {
  const list = $("#customer-list");
  list.replaceChildren();
  for (const person of await api("/api/customers")) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "btn";
    button.textContent = person.name;
    button.addEventListener("click", async () => {
      customer = await api("/api/session", {
        method: "POST",
        body: JSON.stringify({ customer_id: person.id }),
      });
      go("#/accounts");
    });
    const item = document.createElement("li");
    item.append(button);
    list.append(item);
  }
  show("choose");
}

async function renderAccounts() {
  accounts = await api("/api/accounts");
  const rows = accounts.map((account) => {
    const link = document.createElement("a");
    link.href = `#/statement/${account.id}`;
    link.textContent = "Statement";
    const tr = document.createElement("tr");
    tr.append(
      cell(account.number, "nowrap"),
      cell(account.currency, "hide-small"),
      cell(`${account.balance} ${account.currency}`, "num"),
      document.createElement("td"),
    );
    tr.lastChild.append(link);
    return tr;
  });
  $("#account-rows").replaceChildren(...rows);
  show("accounts");
}

async function renderTransfer() {
  accounts = await api("/api/accounts");
  const select = $("#transfer-form select");
  const chosen = select.value;
  select.replaceChildren(
    ...accounts.map((account) => {
      const option = document.createElement("option");
      option.value = account.id;
      option.textContent = `${account.number} (${account.balance} ${account.currency})`;
      return option;
    }),
  );
  if (chosen) select.value = chosen;
  show("transfer");
}

async function sendTransfer(event) {
  event.preventDefault();
  const form = event.target;
  const status = $("#transfer-status");
  const button = $("#send-button");
  const data = Object.fromEntries(new FormData(form));
  const body = {
    from_account_id: Number(data.from_account_id),
    to_account_number: data.to_account_number.trim(),
    amount: data.amount.trim(),
    reference: data.reference.trim() || null,
  };
  button.disabled = true;
  try {
    const result = await api("/api/transfers", { method: "POST", body: JSON.stringify(body) });
    status.className = "notice notice-ok";
    status.textContent = `Transfer sent: ${result.amount} ${result.currency} to ${result.to_account_number}.`;
    form.amount.value = "";
    form.reference.value = "";
    await renderTransfer();
  } catch (error) {
    status.className = "notice notice-error";
    status.textContent = `Transfer failed: ${error.message}`;
  } finally {
    status.hidden = false;
    button.disabled = false;
  }
}

async function renderStatement(accountId, from, to) {
  const query = new URLSearchParams();
  if (from) query.set("from", from);
  if (to) query.set("to", to);
  const st = await api(`/api/accounts/${accountId}/statement?${query}`);
  const cur = st.account.currency;
  const form = $("#statement-form");
  form.dataset.account = accountId;
  form.from.value = st.from;
  form.to.value = st.to;
  $("#statement-number").textContent = st.account.number;
  $("#statement-summary").textContent =
    `${st.from} to ${st.to}: opening ${st.opening_balance} ${cur}, ` +
    `closing ${st.closing_balance} ${cur}, ${st.lines.length} lines.`;
  const opening = document.createElement("tr");
  opening.append(cell(st.from, "nowrap"), cell("Opening balance"), cell(""), cell(st.opening_balance, "num"));
  const rows = st.lines.map((line) => {
    const tr = document.createElement("tr");
    const description = line.reference ? `${line.description} (${line.reference})` : line.description;
    tr.append(
      cell(line.posted_at.slice(0, 10), "nowrap"),
      cell(description),
      cell(line.amount, line.amount_minor < 0 ? "num neg" : "num"),
      cell(line.balance, "num"),
    );
    return tr;
  });
  $("#statement-rows").replaceChildren(opening, ...rows);
  show("statement");
}

async function route() {
  showError("");
  try {
    if (!customer) await loadSession();
    const [, page, id] = location.hash.split("/");
    if (!customer || page === "choose") return await renderChoose();
    if (page === "transfer") return await renderTransfer();
    if (page === "statement" && id) return await renderStatement(id);
    return await renderAccounts();
  } catch (error) {
    if (error.status === 401) {
      customer = null;
      return renderChoose();
    }
    showError(error.message);
  }
}

$("#transfer-form").addEventListener("submit", sendTransfer);
$("#statement-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.target;
  renderStatement(form.dataset.account, form.from.value, form.to.value).catch((error) =>
    showError(error.message),
  );
});
$("#switch-customer").addEventListener("click", async () => {
  await api("/api/session", { method: "DELETE" });
  customer = null;
  go("#/choose");
});
window.addEventListener("hashchange", route);
route();
