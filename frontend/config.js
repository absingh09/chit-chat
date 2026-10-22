// The same files work for local testing AND for the deployed site.
//  - Opened from localhost / 127.0.0.1  -> uses your local backend
//  - Opened from your Vercel URL        -> uses your Render backend
const isLocal = ["localhost", "127.0.0.1"].includes(window.location.hostname);

window.CHAT_BACKEND_URL = isLocal
  ? "http://127.0.0.1:8000"
  : "https://chit-chat-vw95.onrender.com"; // <- put your real Render URL here (no trailing slash)

// Supabase public settings (the anon key is meant to be public).
// NEVER put the service_role / secret key in this file.
window.SUPABASE_URL = "https://tnvrcpdsnvkfzwcyliab.supabase.co";
window.SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRudnJjcGRzbnZrZnp3Y3lsaWFiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTI1ODE0NzQsImV4cCI6MjEwODE1NzQ3NH0.bz85_GNK_8AVfxzI8mauYYq_-x79OwL63bth5RFhd8E";