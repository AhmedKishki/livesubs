import assert from "node:assert/strict";
import test from "node:test";
import { buildLanguageButtons } from "../static/viewer.js";

test("language buttons are visible native-labelled navigation with accessible active state", () => {
  globalThis.document = { createElement(tag) {
    return { tag, attrs: {}, children: [], handlers: {},
      setAttribute(key, value) { this.attrs[key] = value; },
      append(child) { this.children.push(child); },
      addEventListener(type, callback) { this.handlers[type] = callback; } };
  } };
  let navigated;
  globalThis.window = { location: { href: "http://host/s/demo?x=1#f", assign(url) { navigated = url; } } };
  const session = { default: "en", languages: ["en", "de", "ar"], targets: [
    {code:"en", native:"English"}, {code:"de", native:"Deutsch"}, {code:"ar", native:"العربية"},
  ] };
  const group = buildLanguageButtons(session, "en", {language:"Language"});
  assert.equal(group.tag, "nav");
  assert.equal(group.attrs["aria-label"], "Language");
  assert.equal(group.className, "viewer-languages");
  assert.deepEqual(group.children.map(b => b.textContent), ["English", "Deutsch", "العربية"]);
  assert.deepEqual(group.children.map(b => b.attrs["aria-pressed"]), ["true", "false", "false"]);
  for (const button of group.children) {
    assert.equal(button.type, "button");
    assert.equal(button.attrs["data-language"], button.lang);
    button.handlers.click();
    assert.equal(navigated, `/s/demo?x=1&lang=${button.lang}#f`);
  }
});
