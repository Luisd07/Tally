"use strict";

// Throwaway preview of the Tally API. Plain DOM calls, no framework; data is only ever
// inserted as text nodes, never parsed as markup.

const BUCKETS = ["personal", "business", "reimbursable", "transfer", "income"];

// The token arrives in the URL fragment (never sent to the server) and lives in
// sessionStorage for this tab only.
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.get("token")) {
  sessionStorage.setItem("token", fragment.get("token"));
  history.replaceState(null, "", location.pathname);
}

const view = document.getElementById("view");
const errorBox = document.getElementById("error");

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key in el) el[key] = value;
    else el.setAttribute(key, value);
  }
  el.append(...children.flat().filter((c) => c !== null && c !== undefined));
  return el;
}

function showError(err) {
  errorBox.textContent = err.message;
  errorBox.hidden = false;
}

async function api(method, path, { params, body } = {}) {
  const token = sessionStorage.getItem("token");
  if (!token) throw new Error("No token. Run `uv run python -m scripts.dev_token` and open the printed URL.");
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== "" && value !== null && value !== undefined) query.set(key, value);
  }
  const qs = query.toString();
  const response = await fetch(path + (qs ? "?" + qs : ""), {
    method,
    headers: {
      Authorization: "Bearer " + token,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = data && data.error;
    let message = error ? `${response.status} ${error.code}: ${error.message}` : `HTTP ${response.status}`;
    for (const detail of (error && error.details) || []) {
      message += `\n  ${(detail.loc || []).join(".")}: ${detail.msg}`;
    }
    throw new Error(message);
  }
  return data;
}

// Run an action, showing any API error instead of throwing into the console.
async function guard(action) {
  errorBox.hidden = true;
  try {
    await action();
  } catch (err) {
    showError(err);
  }
}

function localMonth() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

const money = (value) => (value === null || value === undefined ? "" : Number(value).toFixed(2));
const moneyCell = (value) => h("td", { className: "num" }, money(value));
const table = (head, rows) =>
  h("table", {}, h("thead", {}, h("tr", {}, head.map((t) => h("th", {}, t)))), h("tbody", {}, rows));
const monthInput = (value, onchange) =>
  h("input", { type: "month", value, onchange: (e) => e.target.value && onchange(e.target.value) });
const labelled = (text, input) => h("label", {}, text + " ", input);

// Categories and accounts are fetched once; every view needs their names.
let refs = null;
async function loadRefs() {
  if (!refs) {
    const [categories, accounts] = await Promise.all([api("GET", "/api/categories"), api("GET", "/api/accounts")]);
    refs = {
      categories,
      accounts,
      accountName: (id) => (accounts.find((a) => a.id === id) || {}).name || "",
    };
  }
}

function select(options, current, placeholder) {
  const el = h("select", {}, placeholder === undefined ? null : h("option", { value: "" }, placeholder));
  for (const [value, label] of options) el.append(h("option", { value, selected: value === current }, label));
  return el;
}

const categorySelect = (current) => select(refs.categories.map((c) => [c.id, c.name]), current, "(none)");
const bucketSelect = (current) => select(BUCKETS.map((b) => [b, b]), current);

const TXN_HEAD = ["Date", "Description", "Account", "Amount", "Category", "Bucket", "Notes", ""];

// One editable transaction row, shared by the Inbox and the Transactions list.
// `confirm` also sends reviewed=true (Inbox); inbox suggestions pre-fill unset selects.
function txnRow(txn, { confirm, onSaved }) {
  const category = categorySelect(txn.category_id || txn.suggested_category_id);
  const bucket = bucketSelect(confirm && txn.suggested_bucket ? txn.suggested_bucket : txn.bucket);
  const notes = h("input", { type: "text", value: txn.notes || "" });
  const save = h("button", {
    textContent: confirm ? "Confirm" : "Save",
    onclick: () =>
      guard(async () => {
        const body = { category_id: category.value || null, bucket: bucket.value, notes: notes.value || null };
        if (confirm) body.reviewed = true;
        await api("PATCH", `/api/transactions/${txn.id}`, { body });
        save.textContent = "Saved";
        await onSaved();
      }),
  });
  const flags = [txn.pending && "pending", txn.is_split && "split", txn.needs_resplit && "needs resplit", txn.unmatched_manual && "unmatched manual"]
    .filter(Boolean)
    .join(", ");
  return h(
    "tr",
    {},
    h("td", {}, txn.posted_date),
    h("td", {}, txn.merchant || txn.description_raw, flags ? h("div", { className: "muted" }, flags) : null),
    h("td", {}, refs.accountName(txn.account_id)),
    moneyCell(txn.amount),
    h("td", {}, category),
    h("td", {}, bucket),
    h("td", {}, notes),
    h("td", {}, save),
  );
}

const views = {
  async home(root) {
    const out = h("div");
    const load = (month) =>
      guard(async () => {
        const home = await api("GET", "/api/home", { params: { month } });
        out.replaceChildren(
          table(["Bucket", "Spent"], home.spending_by_bucket.buckets.map((b) => h("tr", {}, h("td", {}, b.bucket), moneyCell(b.spent)))),
          h("p", {}, `Total spent: ${money(home.spending_by_bucket.total)}`),
          h("p", {}, `Owed to you: ${money(home.owed_to_you)}`),
          h("p", {}, `Income: ${money(home.income.received)} received of ${money(home.income.expected)} expected`),
          h("h3", {}, "Bills due in the next 7 days"),
          table(["Kind", "Name", "Due", "Amount"], home.bills_due.map((b) => h("tr", {}, h("td", {}, b.kind), h("td", {}, b.name), h("td", {}, b.due_date), moneyCell(b.amount)))),
          h("p", { className: "muted" }, `Last updated: ${home.last_updated || "never"}`),
        );
      });
    root.append(h("h2", {}, "Home"), h("div", { className: "controls" }, labelled("Month", monthInput(localMonth(), load))), out);
    await load(localMonth());
  },

  async inbox(root) {
    await loadRefs();
    const out = h("div");
    const load = () =>
      guard(async () => {
        const page = await api("GET", "/api/inbox", { params: { limit: 500 } });
        out.replaceChildren(
          h("p", {}, `${page.total} item(s) need attention. Pick a category and bucket, then Confirm.`),
          table(TXN_HEAD, page.items.map((t) => txnRow(t, { confirm: true, onSaved: load }))),
        );
      });
    root.append(h("h2", {}, "Inbox"), out);
    await load();
  },

  async transactions(root) {
    await loadRefs();
    const filters = { month: localMonth(), bucket: "", account_id: "" };
    const out = h("div");
    const load = () =>
      guard(async () => {
        const rows = h("tbody");
        const more = h("button", { textContent: "Load more", hidden: true });
        let cursor = null;
        const fetchPage = () =>
          guard(async () => {
            const page = await api("GET", "/api/transactions", { params: { ...filters, limit: 100, cursor } });
            rows.append(...page.items.map((t) => txnRow(t, { confirm: false, onSaved: () => {} })));
            cursor = page.next_cursor;
            more.hidden = !cursor;
          });
        more.onclick = fetchPage;
        out.replaceChildren(h("table", {}, h("thead", {}, h("tr", {}, TXN_HEAD.map((t) => h("th", {}, t)))), rows), more);
        await fetchPage();
      });
    const bucketFilter = select(BUCKETS.map((b) => [b, b]), "", "(all)");
    const accountFilter = select(refs.accounts.map((a) => [a.id, a.name]), "", "(all)");
    bucketFilter.onchange = () => {
      filters.bucket = bucketFilter.value;
      load();
    };
    accountFilter.onchange = () => {
      filters.account_id = accountFilter.value;
      load();
    };
    root.append(
      h("h2", {}, "Transactions"),
      h(
        "div",
        { className: "controls" },
        labelled("Month", monthInput(filters.month, (m) => {
          filters.month = m;
          load();
        })),
        labelled("Bucket", bucketFilter),
        labelled("Account", accountFilter),
      ),
      out,
    );
    await load();
  },

  async dashboard(root) {
    const state = { month: localMonth(), months: 6 };
    const out = h("div");
    const load = () =>
      guard(async () => {
        const d = await api("GET", "/api/dashboard", { params: state });
        out.replaceChildren(
          table(
            ["Category", "Budget", "Spent", "Remaining"],
            d.by_category.map((c) => h("tr", {}, h("td", {}, c.name || "(uncategorized)"), moneyCell(c.budget), moneyCell(c.spent), moneyCell(c.remaining))),
          ),
          h("p", {}, `Total spent ${money(d.total_spent)} of ${money(d.total_budget)} budgeted`),
          h("h3", {}, "Trend"),
          table(["Month", "Spent"], d.trend.map((t) => h("tr", {}, h("td", {}, t.month), moneyCell(t.spent)))),
        );
      });
    const months = h("input", { type: "number", min: 1, max: 24, value: state.months });
    months.onchange = () => {
      state.months = months.value;
      load();
    };
    root.append(
      h("h2", {}, "Dashboard"),
      h(
        "div",
        { className: "controls" },
        labelled("Month", monthInput(state.month, (m) => {
          state.month = m;
          load();
        })),
        labelled("Trend months", months),
      ),
      out,
    );
    await load();
  },

  async cards(root) {
    const log = h("div");
    const showLog = (card) =>
      guard(async () => {
        const body = h("div");
        const load = (month) =>
          guard(async () => {
            const l = await api("GET", `/api/accounts/${card.account.id}/card-log`, { params: { month } });
            body.replaceChildren(
              table(
                ["Line", "Value"],
                Object.entries(l).map(([k, v]) => h("tr", {}, h("td", {}, k), h("td", { className: "num" }, v === null ? "" : String(v)))),
              ),
            );
          });
        log.replaceChildren(h("h3", {}, `Card log: ${card.account.name}`), h("div", { className: "controls" }, labelled("Month", monthInput(localMonth(), load))), body);
        await load(localMonth());
      });
    const cards = await api("GET", "/api/accounts/cards");
    root.append(
      h("h2", {}, "Cards"),
      table(
        ["Card", "Balance", "As of", "Due day", "Planned payment", "APR", "Source", ""],
        cards.map((c) =>
          h(
            "tr",
            {},
            h("td", {}, c.account.name),
            moneyCell(c.balance),
            h("td", {}, c.balance_date || ""),
            h("td", {}, c.account.due_day ?? ""),
            moneyCell(c.account.planned_payment),
            h("td", { className: "num" }, c.account.apr ?? ""),
            h("td", {}, c.source_hint),
            h("td", {}, h("button", { textContent: "Card log", onclick: () => showLog(c) })),
          ),
        ),
      ),
      log,
    );
  },

  async reimbursements(root) {
    const out = h("div");
    const load = () =>
      guard(async () => {
        const [owed, reports, txns] = await Promise.all([
          api("GET", "/api/reimbursements/owed"),
          api("GET", "/api/reimbursements"),
          api("GET", "/api/transactions", { params: { bucket: "reimbursable", limit: 500 } }),
        ]);
        const picks = txns.items.filter((t) => !t.reimbursement_id).map((t) => ({ t, box: h("input", { type: "checkbox" }) }));
        const note = h("input", { type: "text", placeholder: "note" });
        const today = new Date().toLocaleDateString("en-CA");
        const submit = (r) =>
          h("button", {
            textContent: "Submit",
            onclick: () =>
              guard(async () => {
                await api("PATCH", `/api/reimbursements/${r.id}`, { body: { status: "submitted", submitted_date: today } });
                await load();
              }),
          });
        out.replaceChildren(
          h("p", {}, `Owed to you: ${money(owed.owed)}`),
          h("h3", {}, "Reports"),
          table(
            ["Status", "Note", "Total", "Transactions", "Submitted", "Paid", ""],
            reports.items.map((r) =>
              h(
                "tr",
                {},
                h("td", {}, r.status),
                h("td", {}, r.note || ""),
                moneyCell(r.total),
                h("td", { className: "num" }, r.transaction_count),
                h("td", {}, r.submitted_date || ""),
                h("td", {}, r.paid_date || ""),
                h("td", {}, r.status === "draft" ? submit(r) : null),
              ),
            ),
          ),
          h("h3", {}, "New report from reimbursable transactions not yet in a report"),
          table(["", "Date", "Description", "Amount"], picks.map(({ t, box }) => h("tr", {}, h("td", {}, box), h("td", {}, t.posted_date), h("td", {}, t.merchant || t.description_raw), moneyCell(t.amount)))),
          note,
          h("button", {
            textContent: "Create draft report",
            onclick: () =>
              guard(async () => {
                const body = { id: crypto.randomUUID(), note: note.value || null, transaction_ids: picks.filter((p) => p.box.checked).map((p) => p.t.id) };
                await api("POST", "/api/reimbursements", { body });
                await load();
              }),
          }),
        );
      });
    root.append(h("h2", {}, "Reimbursements"), out);
    await load();
  },

  async tax(root) {
    const out = h("div");
    const load = (year) =>
      guard(async () => {
        const [tax, missing] = await Promise.all([
          api("GET", "/api/tax/summary", { params: { year } }),
          api("GET", "/api/tax/missing-receipts", { params: { year, limit: 1 } }),
        ]);
        out.replaceChildren(
          h("p", {}, `Gross receipts: ${money(tax.gross_receipts)}. Total deductible: ${money(tax.total_deductible)}. Receipts missing: ${missing.count}.`),
          table(
            ["Schedule C line", "Spent", "Deductible %", "Deductible"],
            tax.expenses_by_line.map((l) => h("tr", {}, h("td", {}, l.tax_line), moneyCell(l.spent), h("td", { className: "num" }, l.deductible_pct ?? ""), moneyCell(l.deductible))),
          ),
          h("p", {}, `Uncategorized business spend: ${money(tax.uncategorized_business.spent)} (${tax.uncategorized_business.count} transactions)`),
          h("ul", {}, tax.notes.map((n) => h("li", {}, n))),
        );
      });
    const year = h("input", { type: "number", min: 1900, max: 2100, value: new Date().getFullYear() });
    year.onchange = () => load(year.value);
    root.append(h("h2", {}, "Tax summary"), h("div", { className: "controls" }, labelled("Year", year)), out);
    await load(year.value);
  },

  async sync(root) {
    const page = await api("GET", "/api/sync/runs", { params: { limit: 50 } });
    root.append(
      h("h2", {}, "Sync runs"),
      table(
        ["Started", "Finished", "Status", "Trigger", "New", "Updated", "Errors"],
        page.items.map((r) =>
          h(
            "tr",
            {},
            h("td", {}, r.started_at),
            h("td", {}, r.finished_at || ""),
            h("td", {}, r.status),
            h("td", {}, r.trigger),
            h("td", { className: "num" }, r.new_count),
            h("td", { className: "num" }, r.updated_count),
            h("td", {}, r.errors.length ? h("pre", {}, JSON.stringify(r.errors, null, 2)) : null),
          ),
        ),
      ),
    );
  },
};

const NAMES = {
  home: "Home",
  inbox: "Inbox",
  transactions: "Transactions",
  dashboard: "Dashboard",
  cards: "Cards",
  reimbursements: "Reimbursements",
  tax: "Tax",
  sync: "Sync runs",
};
const nav = document.getElementById("nav");

function show(name) {
  for (const button of nav.children) button.classList.toggle("active", button.dataset.view === name);
  view.replaceChildren();
  guard(() => views[name](view));
}

for (const [name, label] of Object.entries(NAMES)) {
  nav.append(h("button", { textContent: label, "data-view": name, onclick: () => show(name) }));
}
show("home");
