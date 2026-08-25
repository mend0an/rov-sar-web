"""
Enhancement utilities — DIPINDAH APA ADANYA dari project PyQt5 lama.

File ini SAMA PERSIS dengan `enhancement_utils.py` di project lama.
Sengaja tidak dimodifikasi supaya hasilnya identik dengan versi desktop.

Kalau ke depan mau pakai versi v2 (DCP dengan guided filter), tinggal
copy `enhancement_utils_v2.py` lalu ganti import di `capture.py`.
"""
import cv2
import numpy as np


def calculate_hop_coefficients():
    """
    Menghitung koefisien polinomial untuk metode HOP (Pujiono et al., 2021).
    """
    rb = np.array([215.85/165.76, 215.85/151.38, 215.85/94.94, 215.85/116.56,
                   215.85/108.32, 215.85/79.75, 215.85/35.66])
    gb = np.array([192.61/201.13, 192.61/218.60, 192.61/189.85, 192.61/224.85,
                   192.61/226.45, 192.61/215.42, 192.61/183.57])
    bb = np.array([199.95/217.17, 199.95/218.47, 199.95/191.76, 199.95/229.20,
                   199.95/235.67, 199.95/216.04, 199.95/194.86])

    pk = np.arange(1, 8)
    a = np.vander(pk, N=7, increasing=True)

    rx = np.linalg.lstsq(a, rb, rcond=None)[0]
    gx = np.linalg.lstsq(a, gb, rcond=None)[0]
    bx = np.linalg.lstsq(a, bb, rcond=None)[0]

    return rx, gx, bx


def apply_hop_enhancement(frame, depth, rx, gx, bx):
    """Penerapan HOP enhancement (formula I_p = I_k * K(k))."""
    try:
        import torch
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        frame_tensor = torch.from_numpy(frame).float().to(device)
        poly = torch.tensor([depth**i for i in range(7)], dtype=torch.float32, device=device)

        rx_t = torch.tensor(rx, dtype=torch.float32, device=device)
        gx_t = torch.tensor(gx, dtype=torch.float32, device=device)
        bx_t = torch.tensor(bx, dtype=torch.float32, device=device)

        r_corr = frame_tensor[:, :, 2] * torch.dot(poly, rx_t)
        g_corr = frame_tensor[:, :, 1] * torch.dot(poly, gx_t)
        b_corr = frame_tensor[:, :, 0] * torch.dot(poly, bx_t)

        enhanced = torch.stack([
            b_corr.clamp(0, 255),
            g_corr.clamp(0, 255),
            r_corr.clamp(0, 255),
        ], dim=2).to(torch.uint8).cpu().numpy()
        return enhanced
    except Exception:
        # Fallback CPU NumPy jika torch tidak tersedia atau DLL diblokir oleh OS
        poly = np.array([depth**i for i in range(7)], dtype=np.float32)
        r_scale = float(np.dot(poly, rx))
        g_scale = float(np.dot(poly, gx))
        b_scale = float(np.dot(poly, bx))

        frame_f = frame.astype(np.float32)
        b_corr = np.clip(frame_f[:, :, 0] * b_scale, 0, 255)
        g_corr = np.clip(frame_f[:, :, 1] * g_scale, 0, 255)
        r_corr = np.clip(frame_f[:, :, 2] * r_scale, 0, 255)

        return np.stack([b_corr, g_corr, r_corr], axis=2).astype(np.uint8)


def apply_clahe(frame):
    """CLAHE pada channel L di color space LAB (Zuiderveld, 1994)."""
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l2 = clahe.apply(l)
    lab = cv2.merge((l2, a, b))
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def dehaze_dcp(frame, omega=0.95, t_min=0.1, win_size=15):
    """Dehazing menggunakan Dark Channel Prior (He et al., 2011)."""
    I = frame.astype(np.float32) / 255.0
    h, w, c = I.shape

    min_channel = np.min(I, axis=2)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (win_size, win_size))
    dark = cv2.erode(min_channel, kernel)

    numpx = h * w
    top_count = max(int(numpx * 0.001), 1)
    dark_vec = dark.reshape(numpx)
    img_vec = I.reshape(numpx, 3)
    indices = dark_vec.argsort()[::-1][:top_count]
    A = np.mean(img_vec[indices], axis=0)

    t = 1 - omega * cv2.erode(np.min(I / A, axis=2), kernel)
    t = np.clip(t, t_min, 1)

    J = np.empty_like(I)
    for c in range(3):
        J[:, :, c] = (I[:, :, c] - A[c]) / t + A[c]

    J = np.clip(J, 0, 1)
    return (J * 255).astype(np.uint8)


def apply_white_balance(frame):
    """White Balance Gray World (Buchsbaum, 1980)."""
    result = frame.astype(np.float32)
    avg_b = np.mean(result[:, :, 0])
    avg_g = np.mean(result[:, :, 1])
    avg_r = np.mean(result[:, :, 2])
    avg_gray = (avg_b + avg_g + avg_r) / 3

    eps = 1e-6
    scale_b = avg_gray / (avg_b + eps)
    scale_g = avg_gray / (avg_g + eps)
    scale_r = avg_gray / (avg_r + eps)

    result[:, :, 0] = np.clip(result[:, :, 0] * scale_b, 0, 255)
    result[:, :, 1] = np.clip(result[:, :, 1] * scale_g, 0, 255)
    result[:, :, 2] = np.clip(result[:, :, 2] * scale_r, 0, 255)

    return result.astype(np.uint8)
