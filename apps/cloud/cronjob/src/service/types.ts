export enum EnumIllustType {
  IllustUnknown = -1,
  IllustTypeJpg = 0,
  IllustTypeGif = 2,
}

export interface Translation {
  en?: string;
  zh?: string;
}

export interface Tags {
  tags: Tag[];
}

export interface Tag {
  tag: string;
  locked: boolean;
  deletable: boolean;
  userId: string;
  userName: string;
  translation?: Translation;
}

export interface GetIllustInfoBody {
  tags?: Tags;
  userName: string;
  userId: string;
  illustTitle: string;
  illustType: EnumIllustType;
}
