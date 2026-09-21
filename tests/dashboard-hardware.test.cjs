const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const script = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const logic = script.slice(0, script.indexOf('\nDIMS.forEach(dim => {'));

function context(hash = "") {
  const location = { hash, pathname: "/" };
  const sandbox = vm.createContext({
    URLSearchParams,
    location,
    history: { replaceState: (_state, _title, url) => { location.hash = url.startsWith("#") ? url : ""; } },
    document: { getElementById: () => ({}) },
    data: ["Standard_D4s_v3", "Standard_D4s_v6", "Standard_F4as_v7"].map(sku => ({
      runner: `azure-ephemeral-${sku}-Premium_LRS-128gb`,
      scenario: "building_comfort", variant: "drasi_lib", reaction: "building-comfort",
      ts: "2026-09-21T07:00:00Z",
    })),
  });
  vm.runInContext(logic, sandbox);
  return expression => vm.runInContext(expression, sandbox);
}

test("default view includes only v6", () => {
  const evaluate = context();
  assert.equal(evaluate('applyFilters(rows, readState()).length'), 1);
  assert.equal(evaluate('skuOf(applyFilters(rows, readState())[0])'), "Standard_D4s_v6");
});

test("shared SKU selection survives reload and separates series", () => {
  const evaluate = context("#sku=Standard_D4s_v3,Standard_F4as_v7");
  assert.equal(evaluate('applyFilters(rows, readState()).length'), 2);
  assert.equal(evaluate('groupSeries(applyFilters(rows, readState())).length'), 2);
  evaluate('writeState(readState())');
  assert.equal(evaluate('readState().skus.join(",")'), "Standard_D4s_v3,Standard_F4as_v7");
});

test("excluding every SKU remains an empty selection", () => {
  const evaluate = context("#sku=");
  evaluate('writeState(readState())');
  assert.equal(evaluate('applyFilters(rows, readState()).length'), 0);
});

test("disk profiles never share a series and legacy runners stay distinct", () => {
  const evaluate = context();
  assert.equal(evaluate('groupSeries([rows[1], {...rows[1], runner: "azure-ephemeral-Standard_D4s_v6-StandardSSD_LRS-128gb"}]).length'), 2);
  assert.equal(evaluate('skuOf({runner: "ubuntu-latest"})'), "ubuntu-latest");
});

test("GitHub runner is always offered with its display label", () => {
  const evaluate = context("#sku=ubuntu-latest");
  assert.equal(evaluate('hardwareOptions.includes("ubuntu-latest")'), true);
  assert.equal(evaluate('skuLabel("ubuntu-latest")'), "GitHub ubuntu-latest");
  assert.equal(evaluate('hardwareLabel({runner: "ubuntu-latest"})'), "GitHub ubuntu-latest");
  assert.equal(evaluate('applyFilters([{...rows[0], runner: "ubuntu-latest"}, rows[0]], readState()).length'), 1);
  evaluate('writeState(readState())');
  assert.equal(evaluate('readState().skus.join(",")'), "ubuntu-latest");
});