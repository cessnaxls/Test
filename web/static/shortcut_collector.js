/* iOS Shortcuts — Run JavaScript on Web Page */
try {
  completion(document.documentElement.outerHTML);
} catch (e) {
  completion("ERROR: " + String(e));
}
