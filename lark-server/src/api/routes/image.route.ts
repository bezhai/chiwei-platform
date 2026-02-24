import Router from '@koa/router';
import { Context } from 'koa';
import { bearerAuthMiddleware } from '@middleware/auth';
import { traceMiddleware } from '@middleware/trace';
import { botContextMiddleware } from '@middleware/bot-context';
import {
    validateBody,
    imageProcessValidationRules,
    base64ImageUploadValidationRules,
} from '@middleware/validation';
import {
    imageProcessor,
    ImageProcessRequest,
    ImageProcessError,
    Base64ImageUploadRequest,
} from '@core/services/media/image-processor';

const router = new Router({ prefix: '/api/image' });

// 应用中间件
router.use(traceMiddleware);
router.use(botContextMiddleware);
router.use(bearerAuthMiddleware);

/**
 * 图片处理API - 下载并上传到OSS
 * POST /api/image/process
 * Body: { message_id: string, file_key: string }
 */
router.post('/process',
    validateBody(imageProcessValidationRules),
    async (ctx: Context) => {
        try {
            const request = ctx.request.body as ImageProcessRequest;
            const result = await imageProcessor.processImage(request);
            
            ctx.body = result;
        } catch (error) {
            handleImageProcessError(ctx, error);
        }
    }
);

/**
 * base64图片上传API - 上传到飞书获取image_key
 * POST /api/image/upload-base64
 * Body: { base64_data: string }
 */
router.post('/upload-base64',
    validateBody(base64ImageUploadValidationRules),
    async (ctx: Context) => {
        try {
            const request = ctx.request.body as Base64ImageUploadRequest;
            const result = await imageProcessor.uploadBase64Image(request);
            
            ctx.body = result;
        } catch (error) {
            console.error('Base64图片上传错误:', error);
            handleImageProcessError(ctx, error);
        }
    }
);

/**
 * 统一错误处理函数
 */
function handleImageProcessError(ctx: Context, error: unknown): void {
    if (error instanceof ImageProcessError) {
        ctx.status = error.statusCode;
        ctx.body = {
            success: false,
            message: error.message,
            error_code: error.code
        };
        return;
    }
    
    // 处理其他未预期的错误
    console.error('图片处理发生未预期错误:', error);
    ctx.status = 500;
    ctx.body = {
        success: false,
        message: '服务器内部错误，请稍后重试',
        error_code: 'INTERNAL_SERVER_ERROR'
    };
}

export default router;
