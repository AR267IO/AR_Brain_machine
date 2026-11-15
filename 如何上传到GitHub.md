# 怎么把它传到 GitHub

这个文件夹已经是可以直接上传的仓库了（119 个文件，约 0.51 MB）。

## 方式一：网页上传（不用装任何东西）

1. 打开 https://github.com/new
2. Repository name 填 `glm-brain`（或你喜欢的名字）
3. **Private 还是 Public 自己选**（选 Public 就是开源）
4. 不要勾 Add a README / .gitignore / license（我们已经有了）
5. 点 Create repository
6. 新页面点 `uploading an existing file`
7. 把**这个文件夹里的所有东西**（不是文件夹本身）拖进去
8. 下面 Commit changes 点一下

上传完再点 `Releases` → `Create a new release` → Tag 填 `v1.1`，
把 `格拉默脑机_v1.1_便携版.zip` 和 `格拉默脑机_v1.1_安装包.exe` 传上去，
这样别人在 Releases 里就能下成品（仓库本身不放这么大的二进制）。

## 方式二：命令行（要装 git）

```bat
git init
git add .
git commit -m "格拉默脑机 v1.1"
git branch -M main
git remote add origin https://github.com/你的用户名/glm-brain.git
git push -u origin main
```

第一次 push 会要账号密码，密码那一栏填 **Personal Access Token**（不是登录密码）：

1. https://github.com/settings/tokens → Generate new token (classic)
2. 勾选 `repo` 这一项
3. 生成后复制那串 `ghp_xxx`，当密码用

## 注意

- 仓库里**没有** `config.json`，只有 `config.example.json`。
  你自己的标定坐标不进仓库，别人第一次用要自己标一遍。
- `auto/samples/` 里的游戏截图也没上传（涉及游戏画面版权）。
- 打包产物（zip / exe / build 目录）都在 `.gitignore` 里，走 Releases 发布。