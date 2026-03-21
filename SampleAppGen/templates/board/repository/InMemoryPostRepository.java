package com.example.board.repository;

import com.example.board.model.Post;
import org.springframework.stereotype.Repository;

import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

@Repository
public class InMemoryPostRepository {
    private final ConcurrentHashMap<Long, Post> store = new ConcurrentHashMap<>();
    private final AtomicLong sequence = new AtomicLong();

    public List<Post> findAll() {
        List<Post> posts = new ArrayList<>();
        for (Post value : store.values()) {
            posts.add(copy(value));
        }
        posts.sort(Comparator.comparing(Post::getId));
        return posts;
    }

    public Optional<Post> findById(Long id) {
        Post post = store.get(id);
        return post == null ? Optional.empty() : Optional.of(copy(post));
    }

    public Post create(Post post) {
        long id = sequence.incrementAndGet();
        Instant now = Instant.now();
        Post stored = copy(post);
        stored.setId(id);
        stored.setCreatedAt(now);
        stored.setUpdatedAt(now);
        store.put(id, stored);
        return copy(stored);
    }

    public Optional<Post> update(Long id, Post post) {
        Post updated = store.computeIfPresent(id, (key, existing) -> {
            Post next = copy(post);
            next.setId(id);
            next.setCreatedAt(existing.getCreatedAt());
            next.setUpdatedAt(Instant.now());
            return next;
        });
        return updated == null ? Optional.empty() : Optional.of(copy(updated));
    }

    public boolean delete(Long id) {
        return store.remove(id) != null;
    }

    private Post copy(Post source) {
        return new Post(
            source.getId(),
            source.getTitle(),
            source.getContent(),
            source.getAuthor(),
            source.getCreatedAt(),
            source.getUpdatedAt()
        );
    }
}
