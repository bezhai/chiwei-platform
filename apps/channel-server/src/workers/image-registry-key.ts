/**
 * agent-service 用 ImageRegistry(req.message_id) 把 generate_image 产出的图片注册到
 * Redis，key = `image_registry:{全局 internal message_id}`（见 agent-service
 * app/chat/context.py + app/infra/image.py）。chat-response-worker 渲染回复时必须用
 * 这个【同一个全局 id】查 registry，绝不能用插件反查出来的渠道裸 message id
 * —— 那个裸键 agent-service 从来没写过，registry 必 miss，图片被静默吞掉。
 */
export function imageRegistryLookupId(payload: { message_id: string }): string {
    return payload.message_id;
}
