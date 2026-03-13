package repository

import (
	"gorm.io/driver/postgres"
	"gorm.io/gorm"
	"gorm.io/gorm/logger"
)

// OpenReadOnlyDB opens a database connection suitable for read-only ops queries.
// It does NOT run AutoMigrate — this is intentional for safety.
func OpenReadOnlyDB(dsn string) (*gorm.DB, error) {
	return gorm.Open(postgres.Open(dsn), &gorm.Config{
		Logger: logger.Default.LogMode(logger.Warn),
	})
}
