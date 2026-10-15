// 认证相关 API 封装（对应 Go Auth Service /api/v1/auth/*）。

import { request } from "./client";
import type {
  LoginResponse,
  RegisterResponse,
  AuthUserPublic,
} from "../types";

export interface LoginPayload {
  username: string;
  password: string;
}

export interface RegisterPayload {
  username: string;
  email: string;
  password: string;
}

export function register(payload: RegisterPayload): Promise<RegisterResponse> {
  return request<RegisterResponse>(
    "POST",
    "/api/v1/auth/register",
    payload,
    false
  );
}

export function login(payload: LoginPayload): Promise<LoginResponse> {
  return request<LoginResponse>("POST", "/api/v1/auth/login", payload, false);
}

export function logout(): Promise<{ message: string }> {
  return request<{ message: string }>("POST", "/api/v1/auth/logout", {});
}

export function me(): Promise<AuthUserPublic> {
  return request<AuthUserPublic>("GET", "/api/v1/auth/me");
}
