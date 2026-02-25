
// 图片链接接口
export interface ImageLinks {
    url: string;                     // 图片访问 url
    html: string;                    // HTML 格式
    bbcode: string;                  // BBCode 格式
    markdown: string;                // Markdown 格式
    markdown_with_link: string;      // 带链接的 Markdown
    thumbnail_url: string;           // 缩略图 url
    delete_url: string;              // 图片删除 url
}

// 图片数据接口
export interface ImageUploadData {
    key: string;                     // 图片唯一密钥
    name: string;                    // 图片名称
    pathname: string;                // 图片路径名
    origin_name: string;             // 图片原始名
    size: number;                    // 图片大小，单位 KB
    mimetype: string;                // 图片类型
    extension: string;               // 图片拓展名
    md5: string;                     // 图片 md5 值
    sha1: string;                    // 图片 sha1 值
    links: ImageLinks;               // 链接信息
}

// 上传响应接口
export interface UploadImageResponse {
    status: boolean;                 // 状态，true 或 false
    message: string;                 // 描述信息
    data?: ImageUploadData;          // 数据
}
