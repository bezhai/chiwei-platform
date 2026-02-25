// Re-export base validation utilities from shared
export {
    ValidationError,
    ValidationRule,
    ValidationRules,
    validateBody,
    validateQuery,
} from '@inner/shared';

// Business-specific validation rules

/**
 * 图片处理请求验证规则
 */
export const imageProcessValidationRules = {
    message_id: {
        required: false,
        type: 'string' as const,
        minLength: 1,
        maxLength: 200,
        pattern: /^[a-zA-Z0-9_-]+$/,
        custom: (value: unknown) => {
            if (typeof value === 'string' && !value.trim()) {
                return 'message_id 不能为空白字符';
            }
            return true;
        },
    },
    file_key: {
        required: true,
        type: 'string' as const,
        minLength: 1,
        maxLength: 200,
        pattern: /^[a-zA-Z0-9_.-]+$/,
        custom: (value: unknown) => {
            if (typeof value === 'string' && !value.trim()) {
                return 'file_key 不能为空白字符';
            }
            return true;
        },
    },
};

/**
 * base64图片上传验证规则
 */
export const base64ImageUploadValidationRules = {
    base64_data: {
        required: true,
        type: 'string' as const,
        minLength: 1,
        custom: (value: unknown) => {
            if (typeof value !== 'string' || !value.trim()) {
                return 'base64_data 不能为空白字符';
            }

            // 检查是否是有效的 data:image base64 格式
            const base64Regex =
                /^data:image\/(png|jpg|jpeg|gif|bmp|webp);base64,[A-Za-z0-9+/]+(={0,2})?$/;
            if (!base64Regex.test(value)) {
                return 'base64_data 必须是有效的图片 base64 格式（需要包含 data:image/... 前缀）';
            }

            return true;
        },
    },
};
