/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  env: {
    // Production Cloud Run URL — override with NEXT_PUBLIC_API_URL env var at build time
    NEXT_PUBLIC_API_URL:
      process.env.NEXT_PUBLIC_API_URL ??
      "https://shipsafe-agentops-336382452417.us-central1.run.app",
  },
};
module.exports = nextConfig;
