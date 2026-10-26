/* PatchTrack sign-in. First run with an empty user table shows the
   administrator bootstrap form instead of the login form. */

const form = document.getElementById("login-form");
const errorBox = document.getElementById("error");
const title = document.getElementById("form-title");
const hint = document.getElementById("form-hint");
const submit = document.getElementById("submit");
const nameField = document.getElementById("display-name-field");
const passwordInput = document.getElementById("password");

let needsAdmin = false;

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

async function detectMode() {
  try {
    const response = await fetch("/api/session/status", { credentials: "same-origin" });
    const data = await response.json();
    if (data.authenticated) {
      window.location.replace("/");
      return;
    }
    needsAdmin = Boolean(data.needs_admin);
  } catch {
    needsAdmin = false;
  }
  if (needsAdmin) {
    title.textContent = "Create the first administrator";
    hint.textContent =
      "No accounts exist yet. Create the initial administrator for this deployment.";
    submit.textContent = "Create administrator";
    nameField.hidden = false;
    passwordInput.autocomplete = "new-password";
    passwordInput.minLength = 12;
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  submit.disabled = true;
  const payload = {
    username: document.getElementById("username").value.trim(),
    password: passwordInput.value,
  };
  if (needsAdmin) {
    payload.display_name = document.getElementById("display_name").value.trim();
  }
  const endpoint = needsAdmin ? "/api/session/bootstrap" : "/api/session/login";
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      let detail = "Sign-in failed";
      try {
        const body = await response.json();
        detail = typeof body.detail === "string"
          ? body.detail
          : (body.detail?.[0]?.msg || detail);
      } catch { /* keep default */ }
      showError(detail);
      submit.disabled = false;
      return;
    }
    window.location.replace("/");
  } catch {
    showError("The PatchTrack gateway is unreachable. Please try again.");
    submit.disabled = false;
  }
});

detectMode();
