-- 0009 (MySQL 8): tidy up the customers table and retire old data.
# DROP TABLE not_a_statement; this line is a comment

ALTER TABLE `customers` CHANGE COLUMN `fullname` `full_name` VARCHAR(255) NOT NULL;

ALTER TABLE `customers` DROP COLUMN `fax`, DROP INDEX `idx_customers_fax`;

RENAME TABLE `order_items` TO `line_items`;

DELETE FROM `password_resets`;

UPDATE `customers` SET `note` = 'it\'s migrated; DROP TABLE x' WHERE `id` = 1;

ALTER TABLE `customers` MODIFY `email` VARCHAR(320) NOT NULL;

DELIMITER //
CREATE PROCEDURE purge_sessions()
BEGIN
  DELETE FROM sessions;
END//
DELIMITER ;

ALTER TABLE `customers` CHANGE `phone` `phone` VARCHAR(32) NULL;
