// 平台插件清单。import 即触发各插件的自注册副作用(进 ChannelRegistry +
// CommandRegistry)。加平台 = 新增 plugins/<channel> 模块 + 在此 import 一行，
// core 主流程零改动。
//
// 引入顺序即注册顺序；同 channel 重复注册由注册表 fail-closed 抛错。
import './lark';
import './qq';

export {};
