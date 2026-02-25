package domain

import "time"

const DefaultLane = "prod"

// Lane 代表一条部署泳道（如 prod、staging、feature-xxx）。
// prod 是系统预置的默认泳道，不可删除。
type Lane struct {
	Name        string    `json:"name"`
	Description string    `json:"description,omitempty"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}

func (l *Lane) IsDefault() bool {
	return l.Name == DefaultLane
}
