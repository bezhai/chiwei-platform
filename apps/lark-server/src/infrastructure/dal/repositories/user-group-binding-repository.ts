import { Repository, DataSource } from 'typeorm';
import { UserGroupBinding } from '@entities';

export class UserGroupBindingRepository extends Repository<UserGroupBinding> {
    constructor(dataSource: DataSource) {
        super(UserGroupBinding, dataSource.createEntityManager());
    }

    async findByUserAndChat(userUnionId: string, chatId: string): Promise<UserGroupBinding | null> {
        return this.findOne({ where: { userUnionId, chatId } });
    }

    async findByChat(chatId: string): Promise<UserGroupBinding[]> {
        return this.find({ where: { chatId } });
    }

    async findByUser(userUnionId: string): Promise<UserGroupBinding[]> {
        return this.find({ where: { userUnionId } });
    }

    async createBinding(userUnionId: string, chatId: string): Promise<UserGroupBinding> {
        const binding = new UserGroupBinding();
        binding.userUnionId = userUnionId;
        binding.chatId = chatId;
        binding.isActive = true;
        return this.save(binding);
    }

    async deactivateBinding(userUnionId: string, chatId: string): Promise<void> {
        await this.update({ userUnionId, chatId }, { isActive: false });
    }

    async activateBinding(userUnionId: string, chatId: string): Promise<void> {
        await this.update({ userUnionId, chatId }, { isActive: true });
    }
}
