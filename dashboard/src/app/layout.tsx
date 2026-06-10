import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ShipSafe AgentOps",
  description: "Fleet health observability via Dynatrace + OTel",
  icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
