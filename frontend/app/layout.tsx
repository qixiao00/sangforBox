import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "SangforBox · AF 智能工作台",
  description: "深信服 AF 设备知识、诊断与自动化工作台",
  icons: { icon: "/static/sangfor-box.svg" },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
