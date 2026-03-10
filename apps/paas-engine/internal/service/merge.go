package service

import "encoding/json"

// ParseFields 解析 JSON body 为字段 → 原始值映射，用于判断哪些字段实际发送了。
func ParseFields(body []byte) (map[string]json.RawMessage, error) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(body, &fields); err != nil {
		return nil, err
	}
	return fields, nil
}

// ApplyField 如果 key 存在于 fields 中，将其反序列化到 target。
func ApplyField[T any](fields map[string]json.RawMessage, key string, target *T) error {
	v, ok := fields[key]
	if !ok {
		return nil
	}
	return json.Unmarshal(v, target)
}

// MergeEnvs 合并 envs map 字段。
//   - rawEnvs == nil（字段未发送）→ 返回 existing 不变
//   - rawEnvs == "null" → 清空整个 map
//   - rawEnvs == {} → 不变
//   - rawEnvs == {"K":"V"} → 合并 K 到 existing
//   - rawEnvs == {"K":null} → 从 existing 删除 K
func MergeEnvs(existing map[string]string, rawEnvs json.RawMessage) (map[string]string, error) {
	if rawEnvs == nil {
		return existing, nil
	}
	if string(rawEnvs) == "null" {
		return nil, nil
	}
	var patch map[string]*string
	if err := json.Unmarshal(rawEnvs, &patch); err != nil {
		return nil, err
	}
	if len(patch) == 0 {
		return existing, nil
	}
	merged := make(map[string]string, len(existing))
	for k, v := range existing {
		merged[k] = v
	}
	for k, v := range patch {
		if v == nil {
			delete(merged, k)
		} else {
			merged[k] = *v
		}
	}
	return merged, nil
}
