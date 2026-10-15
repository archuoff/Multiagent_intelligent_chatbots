import { useState } from "react";

// Demo-only gate for the frontend prototype. Not real authentication --
// the backend's own auth (CurrentPrincipal) is unaffected and still runs
// independently once past this screen.
const TEST_USERNAME = "demo";
const TEST_PASSWORD = "demo1234";

export default function Login({ onSuccess }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  function handleSubmit(event) {
    event.preventDefault();
    if (username === TEST_USERNAME && password === TEST_PASSWORD) {
      setError("");
      onSuccess();
    } else {
      setError("Incorrect username or password.");
    }
  }

  return (
    <div className="login-screen">
      <form className="login-card" onSubmit={handleSubmit}>
        <div className="login-brand">
          <h1>Tata Technologies</h1>
          <p>JLR Knowledge Platform</p>
        </div>
        <h2>Sign in</h2>
        <label htmlFor="login-username">Username</label>
        <input
          id="login-username"
          type="text"
          autoComplete="username"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          autoFocus
        />
        <label htmlFor="login-password">Password</label>
        <input
          id="login-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
        {error && <p className="login-error">{error}</p>}
        <button type="submit">Sign in</button>
      </form>
    </div>
  );
}
