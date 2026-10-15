import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 前端开发服务器通过代理把 /api 和 /agent 转发到 Go Auth Service。
// 这样开发期同源，无需后端开启 CORS。
// 生产部署时建议用 nginx 等反向代理做同样的路径转发。
//
// 代理目标可用环境变量 VITE_API_TARGET 覆盖，默认 http://127.0.0.1:8080
// （本机 8080 被其它服务占用时，Go Auth Service 通常起在 8080）。
const API_TARGET = process.env.VITE_API_TARGET || "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
      },
      "/agent": {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
    allowedHosts: [ 'astronaut-suds-lung.ngrok-free.dev', ]
  },
});
