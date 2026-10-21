/**
 * WMD DPR Fix
 *
 * 修复 ComfyUI 在 devicePixelRatio > 1 时「节点与背景网格错位 / 画布缩在一角」。
 *
 * 真正根因（不是前端缺陷）
 * ------------------------
 * comfyui-mixlab-nodes 的 web/javascript/td_background.js 整体覆盖了
 * LGraphCanvas.prototype.drawBackCanvas，并在模块加载时求值一次：
 *
 *     let scale = LGraphCanvas.prototype.drawBackCanvas.toString()
 *                         .match('window.devicePixelRatio') ? window.devicePixelRatio : 1
 *
 * 旧版 litegraph 的 drawBackCanvas 源码里含 'window.devicePixelRatio' 字面量，
 * 所以 scale 会被正确赋成当前 dpr；
 * 但新版前端把 drawBackCanvas 压缩成 `let{dpr:e}=this`（没有那个字面量），
 * .match() 返回 null -> scale 永久等于 1。
 *
 * 结果：背景网格按 1 倍绘制，前景节点按真实 dpr 绘制 -> 两者错位。
 *
 * 复现条件（Windows 显示缩放 125%）
 * ---------------------------------
 *   Chrome 页面缩放 75%  -> dpr 0.9375 -> 被 Math.max(dpr,1) 钳成 1
 *                          -> 前景/背景都是 1 倍 -> 恰好正常
 *   Chrome 页面缩放 90%  -> dpr 1.125  -> 前景 1.125 / 背景 1  -> 错位
 *   Chrome 页面缩放 100% -> dpr 1.25   -> 前景 1.25  / 背景 1  -> 错位
 *
 * 修复方式
 * --------
 * 不修改第三方插件源码、不修改前端包。
 * 包装背景画布上下文的 setTransform：当 MixLab 用固定的 1 去做 dpr 变换时，
 * 替换成画布真实的 devicePixelRatio，使前景与背景使用同一倍率。
 *
 * 该包装是幂等的：
 *   - 只在 setTransform 的参数构成「纯 dpr 缩放矩阵」且首参为 1 时才改写；
 *   - 每次上下文被重建时通过 WeakSet 检测并重新包装。
 */

import { app } from "../../scripts/app.js";

const LOG_PREFIX = "[WMD-DPR-Fix]";

// 记录已包装过的上下文，避免重复包装
const wrappedContexts = new WeakSet();

/**
 * 真实 dpr（与前端 normalizeDpr 保持一致：Math.max(dpr, 1)）
 */
function realDpr() {
    if (typeof window === "undefined") return 1;
    const dpr = window.devicePixelRatio;
    if (!(dpr > 0) || !Number.isFinite(dpr)) return 1;
    return Math.max(dpr, 1);
}

/**
 * 包装背景画布上下文的 setTransform，修正 MixLab 硬编码的 dpr=1。
 * @param {CanvasRenderingContext2D} ctx
 */
function patchBackgroundContext(ctx) {
    if (!ctx || typeof ctx.setTransform !== "function") return;
    if (wrappedContexts.has(ctx)) return;
    wrappedContexts.add(ctx);

    const originalSetTransform = ctx.setTransform.bind(ctx);
    ctx.setTransform = function (a, b, c, d, e, f) {
        try {
            // MixLab 的 drawBackCanvas 传入固定的 scale=1 作为 dpr 变换。
            // 纯 dpr 缩放矩阵满足 b=0、c=0、d=a。
            const isPureDprScale =
                (b === 0 || b === undefined) &&
                (c === 0 || c === undefined) &&
                (d === 0 || d === undefined || d === a);
            if (isPureDprScale && a === 1) {
                const target = realDpr();
                if (target !== 1) {
                    return originalSetTransform(target, 0, 0, target, 0, 0);
                }
            }
        } catch (e) {
            // 忽略包装层异常，绝不影响画布正常渲染
        }
        return originalSetTransform(a, b, c, d, e, f);
    };
}

/**
 * 包装 LGraphCanvas.prototype.drawBackCanvas，确保每一帧绘制前背景上下文都已打补丁。
 *
 * 为什么需要包装原型方法：
 *   MixLab 的 td_background.js 直接给 LGraphCanvas.prototype.drawBackCanvas 赋值，
 *   而各扩展的加载顺序不确定。本扩展用 defineProperty 劫持该属性，
 *   无论 MixLab 在本扩展之前还是之后赋值，包装都会生效。
 */
function patchPrototypeDrawBackCanvas() {
    const proto = window.LGraphCanvas?.prototype;
    if (!proto || !("drawBackCanvas" in proto)) return false;

    // 避免重复劫持
    if (proto.__wmdDprPatched) return true;
    proto.__wmdDprPatched = true;

    const rawDescriptor = Object.getOwnPropertyDescriptor(proto, "drawBackCanvas");
    let currentImpl = rawDescriptor?.value ?? proto.drawBackCanvas;

    const makeWrapper = (impl) => function (...args) {
        try {
            // 在真正绘制之前，确保背景上下文已被包装
            patchBackgroundContext(this.bgctx);
        } catch (e) {
            // 忽略
        }
        return impl.apply(this, args);
    };

    let wrapped = makeWrapper(currentImpl);

    Object.defineProperty(proto, "drawBackCanvas", {
        configurable: true,
        enumerable: rawDescriptor?.enumerable ?? true,
        get() {
            return wrapped;
        },
        set(value) {
            currentImpl = value;
            wrapped = makeWrapper(value);
        }
    });

    return true;
}

/**
 * 给当前画布的背景上下文打补丁，并立即重绘一帧使其生效。
 *
 * @param {boolean} resizeCanvas
 *        true 时额外调用 canvas.resize() 让前端重新测量视口并重算前景画布的 dpr。
 *        浏览器页面级缩放变化时，前景画布的 dpr 不会自动更新，必须 resize。
 */
function applyFix(resizeCanvas = false) {
    const canvas = app?.canvas;
    if (!canvas) return false;

    // bgctx 可能尚未创建（首次渲染前）
    if (!canvas.bgctx) {
        canvas.bgctx = canvas.bgcanvas?.getContext("2d");
    }
    if (!canvas.bgctx) return false;

    patchBackgroundContext(canvas.bgctx);

    try {
        if (resizeCanvas && typeof canvas.resize === "function") {
            // canvas.resize() 会让前端 applyViewport() 重新按当前 dpr 缩放前景画布；
            // 背景画布则由上面的 setTransform 包装保证使用同一倍率。
            canvas.resize();
        }
        canvas.draw?.(true, true);
    } catch (e) {
        // 忽略
    }
    return true;
}

/**
 * 检查前景与背景画布的 dpr 是否一致。
 * @returns {boolean}
 */
function isAligned() {
    const canvas = app?.canvas;
    if (!canvas?.ctx || !canvas?.bgctx) return true;
    try {
        return Math.abs(canvas.ctx.getTransform().a - canvas.bgctx.getTransform().a) < 0.001;
    } catch (e) {
        return true;
    }
}

let lastDpr = null;
let scheduled = false;
let pendingResize = false;

function scheduleFix(delay = 200, resizeCanvas = false) {
    if (resizeCanvas) pendingResize = true;
    if (scheduled) return;
    scheduled = true;
    setTimeout(() => {
        scheduled = false;
        const needResize = pendingResize;
        pendingResize = false;
        applyFix(needResize);
    }, delay);
}

/**
 * 轮询检测 devicePixelRatio 变化。
 * 浏览器在页面级缩放变化时不保证触发 resize 事件，
 * 但 devicePixelRatio 会更新，因此用轻量轮询感知。
 */
function startDprWatcher() {
    lastDpr = realDpr();
    setInterval(() => {
        const dpr = realDpr();
        if (Math.abs(dpr - lastDpr) > 0.001) {
            lastDpr = dpr;
            console.log(`${LOG_PREFIX} devicePixelRatio changed to ${dpr}, re-applying fix`);
            // 缩放变化后需要 resize 才能让前景画布重新按新 dpr 绘制
            scheduleFix(400, true);
        }
    }, 500);
}

app.registerExtension({
    name: "Comfy.WmdDprFix",

    async setup() {
        console.log(`${LOG_PREFIX} loaded (dpr = ${realDpr()})`);

        // 0) 立刻劫持 drawBackCanvas，保证第一帧起背景画布就使用正确 dpr
        patchPrototypeDrawBackCanvas();

        // 1) 首屏：前端初始化后画布上下文才存在，延迟打补丁
        setTimeout(() => scheduleFix(500), 0);
        setTimeout(() => scheduleFix(1500), 0);

        // 2) 视口尺寸变化（窗口缩放 / 侧栏开关）后重新打补丁
        window.addEventListener("resize", () => scheduleFix(250));

        // 3) 页面级缩放变化（dpr 变化）
        startDprWatcher();

        // 4) 周期性兜底：确保 bgctx 被重建后仍被包装
        setInterval(() => {
            const canvas = app?.canvas;
            if (canvas?.bgctx && !wrappedContexts.has(canvas.bgctx)) {
                scheduleFix(0);
            }
        }, 5000);

        // 5) 首次检查对齐状态，便于诊断
        setTimeout(() => {
            console.log(`${LOG_PREFIX} canvas dpr aligned: ${isAligned()}`);
        }, 2500);
    }
});
